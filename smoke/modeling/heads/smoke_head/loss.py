import torch
from torch.nn import functional as F

from smoke.modeling.smoke_coder import SMOKECoder
from smoke.layers.focal_loss import FocalLoss
from smoke.layers.utils import select_point_of_interest


class SMOKELossComputation():
    def __init__(self,
                 smoke_coder,
                 cls_loss,
                 reg_loss,
                 loss_weight,
                 max_objs):
        self.smoke_coder = smoke_coder
        self.cls_loss = cls_loss
        self.reg_loss = reg_loss
        self.loss_weight = loss_weight
        self.max_objs = max_objs

    def prepare_targets(self, targets):
        heatmaps = torch.stack([t.get_field("hm") for t in targets])    # torch.Size([1, 3, 96, 320]) 3个通道的heatmaps
        regression = torch.stack([t.get_field("reg") for t in targets]) # torch.Size([1, 30, 3, 8]) 最多30个物体，3个类别，8个坐标
        cls_ids = torch.stack([t.get_field("cls_ids") for t in targets])    # torch.Size([1, 30])   # 30个框对应的类别
        proj_points = torch.stack([t.get_field("proj_p") for t in targets]) # torch.Size([1, 30, 2]) 30个框的中心点投影坐标
        dimensions = torch.stack([t.get_field("dimensions") for t in targets]) # torch.Size([1, 30, 3])
        locations = torch.stack([t.get_field("locations") for t in targets])   # torch.Size([1, 30, 3]) 中心点 相机坐标系
        rotys = torch.stack([t.get_field("rotys") for t in targets])    # torch.Size([1, 30])
        trans_mat = torch.stack([t.get_field("trans_mat") for t in targets])    # torch.Size([1, 3, 3])
        K = torch.stack([t.get_field("K") for t in targets])    # torch.Size([1, 3, 3]) 3*3内参
        reg_mask = torch.stack([t.get_field("reg_mask") for t in targets])  # torch.Size([1, 30])
        flip_mask = torch.stack([t.get_field("flip_mask") for t in targets])    # torch.Size([1, 30])

        return heatmaps, regression, dict(cls_ids=cls_ids,
                                          proj_points=proj_points,
                                          dimensions=dimensions,
                                          locations=locations,
                                          rotys=rotys,
                                          trans_mat=trans_mat,
                                          K=K,
                                          reg_mask=reg_mask,
                                          flip_mask=flip_mask)

    def prepare_predictions(self, targets_variables, pred_regression):
        batch, channel = pred_regression.shape[0], pred_regression.shape[1] # channel = 8
        targets_proj_points = targets_variables["proj_points"]  # torch.Size([1, 30, 2])

        # obtain prediction from points of interests    只对gt物体框对应的中心点的回归值做loss
        pred_regression_pois = select_point_of_interest(
            batch, targets_proj_points, pred_regression
        )
        pred_regression_pois = pred_regression_pois.view(-1, channel)   # bsz*bbox, 8个回归值

        # FIXME: fix hard code here
        pred_depths_offset = pred_regression_pois[:, 0]     # torch.Size([30])
        pred_proj_offsets = pred_regression_pois[:, 1:3]    # torch.Size([30, 2])
        pred_dimensions_offsets = pred_regression_pois[:, 3:6]   # torch.Size([30, 3])
        pred_orientation = pred_regression_pois[:, 6:]          # torch.Size([30, 2])

        pred_depths = self.smoke_coder.decode_depth(pred_depths_offset) #torch.Size([30])
        pred_locations = self.smoke_coder.decode_location(      # 预测得到的3D坐标是 图像平面的GT中心+预测地offset 经过内外参转到世界坐标系 torch.Size([30, 3])
            targets_proj_points,
            pred_proj_offsets,
            pred_depths,
            targets_variables["K"],
            targets_variables["trans_mat"]
        )
        pred_dimensions = self.smoke_coder.decode_dimension(    # torch.Size([30, 3])预测得到的框的长宽高3个维度
            targets_variables["cls_ids"],
            pred_dimensions_offsets,
        )
        # we need to change center location to bottom location
        pred_locations[:, 1] += pred_dimensions[:, 1] / 2

        pred_rotys = self.smoke_coder.decode_orientation(
            pred_orientation,
            targets_variables["locations"],
            targets_variables["flip_mask"]
        )

        if self.reg_loss == "DisL1":
            pred_box3d_rotys = self.smoke_coder.encode_box3d(
                pred_rotys,
                targets_variables["dimensions"],
                targets_variables["locations"]
            )
            pred_box3d_dims = self.smoke_coder.encode_box3d(
                targets_variables["rotys"],
                pred_dimensions,
                targets_variables["locations"]
            )
            pred_box3d_locs = self.smoke_coder.encode_box3d(
                targets_variables["rotys"],
                targets_variables["dimensions"],
                pred_locations
            )

            return dict(ori=pred_box3d_rotys,
                        dim=pred_box3d_dims,
                        loc=pred_box3d_locs, )

        elif self.reg_loss == "L1":
            pred_box_3d = self.smoke_coder.encode_box3d(
                pred_rotys,
                pred_dimensions,
                pred_locations
            )
            return pred_box_3d

    def __call__(self, predictions, targets):
        pred_heatmap, pred_regression = predictions[0], predictions[1]  # torch.Size([2, 3, 96, 320]) torch.Size([2, 8, 96, 320])

        targets_heatmap, targets_regression, targets_variables \
            = self.prepare_targets(targets)

        predict_boxes3d = self.prepare_predictions(targets_variables, pred_regression)  # 提取gt点对应的回归值，将回归值整理为预测的3D框的坐标：预测是旋转角，就用旋转角预测值和其他的gt值计算框坐标

        hm_loss = self.cls_loss(pred_heatmap, targets_heatmap) * self.loss_weight[0]    # torch.Size([2, 3, 96, 320])

        targets_regression = targets_regression.view(
            -1, targets_regression.shape[2], targets_regression.shape[3]    # torch.Size([1, 30, 3, 8]) → torch.Size([30, 3, 8])
        )

        reg_mask = targets_variables["reg_mask"].flatten()
        reg_mask = reg_mask.view(-1, 1, 1)
        reg_mask = reg_mask.expand_as(targets_regression)

        if self.reg_loss == "DisL1":
            reg_loss_ori = F.l1_loss(
                predict_boxes3d["ori"] * reg_mask,
                targets_regression * reg_mask,
                reduction="sum") / (self.loss_weight[1] * self.max_objs)

            reg_loss_dim = F.l1_loss(
                predict_boxes3d["dim"] * reg_mask,
                targets_regression * reg_mask,
                reduction="sum") / (self.loss_weight[1] * self.max_objs)

            reg_loss_loc = F.l1_loss(
                predict_boxes3d["loc"] * reg_mask,
                targets_regression * reg_mask,
                reduction="sum") / (self.loss_weight[1] * self.max_objs)

            return hm_loss, reg_loss_ori + reg_loss_dim + reg_loss_loc


def make_smoke_loss_evaluator(cfg):
    smoke_coder = SMOKECoder(
        cfg.MODEL.SMOKE_HEAD.DEPTH_REFERENCE,
        cfg.MODEL.SMOKE_HEAD.DIMENSION_REFERENCE,
        cfg.MODEL.DEVICE,
    )
    focal_loss = FocalLoss(
        cfg.MODEL.SMOKE_HEAD.LOSS_ALPHA,
        cfg.MODEL.SMOKE_HEAD.LOSS_BETA,
    )

    loss_evaluator = SMOKELossComputation(
        smoke_coder,
        cls_loss=focal_loss,
        reg_loss=cfg.MODEL.SMOKE_HEAD.LOSS_TYPE[1],
        loss_weight=cfg.MODEL.SMOKE_HEAD.LOSS_WEIGHT,
        max_objs=cfg.DATASETS.MAX_OBJECTS,
    )

    return loss_evaluator
