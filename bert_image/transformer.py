import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
import random
from .bert import BertEncoder, BertConfig
import torchvision.models as models
from torch.autograd import Variable
from enum import Enum
from collections import OrderedDict
from .guided_filter import SelfGuidedFilter

def split_dict(d, first_predicate):
    """split the dictionary d into 2 dictionaries, first one contains elements validating first_predicate"""
    first, second = OrderedDict(), OrderedDict()
    for key, value in d.items():
        if first_predicate(key):
            first[key] = value
        else:
            second[key] = value
    return first, second

def Jaggi_get_optimizer(model_named_parameters, config):
    """
    Create an optimizer for a given model
    :param model_parameters: a list of parameters to be trained
    :return: Tuple (optimizer, scheduler)
    """
    max_steps = config["epochs"]
    if config["optimizer_cosine_lr"]:
        max_steps *= len(training_loader.dataset) // config["batch_size"] + 1

    if config["optimizer"] == "SGD":
        without_weight_decay, with_weight_decay = split_dict(
            OrderedDict(model_named_parameters),
            lambda name: "attention_spreads" in name or "attention_centers" in name
        )

        optimizer = torch.optim.SGD(
            [
                {"params": with_weight_decay.values()},
                {"params": without_weight_decay.values(), "weight_decay": 0.}
            ],
            lr=config["optimizer_learning_rate"],
            momentum=config["optimizer_momentum"],
            weight_decay=config["optimizer_weight_decay"],
        )
    elif config["optimizer"] == "Adam":
        optimizer = torch.optim.Adam(model_named_parameters.values(), lr=config["optimizer_learning_rate"])
    else:
        raise ValueError("Unexpected value for optimizer")

    if config["optimizer"] == "Adam":
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda e: 1.)
        print("Adam optimizer ignore all learning rate schedules.")
    elif config["optimizer_cosine_lr"]:
        scheduler = linear_warmup_cosine_lr_scheduler(
            optimizer, config["optimizer_warmup_ratio"], max_steps
        )

    else:
        scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer,
            milestones=config["optimizer_decay_at_epochs"],
            gamma=1.0 / config["optimizer_decay_with_factor"],
        )

    return optimizer, scheduler

class ResBottom(nn.Module):
    def __init__(self, origin_model, block_num=1):
        super(ResBottom, self).__init__()
        self.seq = nn.Sequential(*list(origin_model.children())[0 : (4 + block_num)])

    def forward(self, batch):
        return self.seq(batch)

BertImage_config = OrderedDict(
    dataset="Cifar10",
    model="bert",
    load_checkpoint_file=None,
    no_cuda=False,

    # === OPTIMIZER ===
    optimizer="SGD",
    optimizer_cosine_lr=False,
    optimizer_warmup_ratio=0.0,  # period of linear increase for lr scheduler
    optimizer_decay_at_epochs=[80, 150, 250],
    optimizer_decay_with_factor=10.0,
    optimizer_learning_rate=0.1,
    optimizer_momentum=0.9,
    optimizer_weight_decay=0.0001,
    batch_size=320,         #100,300
    epochs=300,
    seed=42,

    # === From BERT ===
    vocab_size_or_config_json_file=-1,
    hidden_size=128,  # 400,768,
    position_encoding_size=-1,              # dimension of the position embedding for relative attention, if -1 will default to  hidden_size
    num_hidden_layers=3,        #2
    num_attention_heads=8,      #9
    intermediate_size=512,
    hidden_act="gelu",
    hidden_dropout_prob=0.1,
    attention_probs_dropout_prob=0.1,
    max_position_embeddings=16,
    type_vocab_size=2,
    initializer_range=0.02,
    layer_norm_eps=1e-12,

    # === BERT IMAGE===
    add_positional_encoding_to_input=False,
    use_learned_2d_encoding=False,
    use_gaussian_attention=True,
    
    share_position_encoding=False,           # share learned relative position encoding for all layers
    use_attention_data=False,                # use attention between pixel values instead of only positional (q.k attention)
    query_positional_score=False,            # use q.r attention (see Ramachandran, 2019)
    
    attention_isotropic_gaussian=False,     #little higher than TRUE
    prune_degenerated_heads=False,           # remove heads with Sigma^{-1} close to 0 or very singular (kappa > 1000) at epoch 0
    reset_degenerated_heads=False,           # reinitialize randomly the heads mentioned above
    fix_original_heads_position=False,       # original heads (not pruned/reinit) position are fixed to their original value
    fix_original_heads_weights=False,        # original heads (not pruned/reinit) value matrix are fixed to their original value
    gaussian_spread_regularizer=0.,          # penalize singular covariance gaussian attention

    gaussian_init_sigma_std=0.01,
    gaussian_init_mu_std=2.,
    attention_gaussian_blur_trick=False,     # use a computational trick for gaussian attention to avoid computing the attention probas
    pooling_concatenate_size=4,              # 2 concatenate the pixels value by patch of pooling_concatenate_size x pooling_concatenate_size to redude dimension
    pooling_use_resnet=False,

    # === LOGGING ===
    only_list_parameters=False,
    num_keep_checkpoints=0,
    plot_attention_positions=True,
    output_dir="./output.tmp",
)

class BertImage(nn.Module):
    """
    Wrapper for a Bert encoder
    """

    def __init__(self, config, num_classes, output_attentions=False):
        super().__init__()

        self.output_attentions = output_attentions
        self.with_resnet = config["pooling_use_resnet"]
        self.hidden_size = config["hidden_size"]
        self.pooling_concatenate_size = config["pooling_concatenate_size"]
        assert (config["pooling_concatenate_size"] == 1) or (
            not config["pooling_use_resnet"]
        ), "Use either resnet or pooling_concatenate_size"


        if self.with_resnet:
            res50 = models.resnet50(pretrained=True)
            self.extract_feature = ResBottom(res50)

            # compute downscale factor and channel at output of ResNet
            _, num_channels_in, new_width, new_height = self.extract_feature(
                torch.rand(1, 3, 1024, 1024)
            ).shape
            self.feature_downscale_factor = 1024 // new_width
        elif self.pooling_concatenate_size > 1:
            num_channels_in = 3 * (self.pooling_concatenate_size ** 2)
        else:
            num_channels_in = 3

        bert_config = BertConfig.from_dict(config)

        self.features_upscale = nn.Linear(num_channels_in, self.hidden_size)
        # self.features_downscale = nn.Linear(self.hidden_size, num_channels_in)
        self.hidden_dims=[16,64,256,512,512,512,512,512]
        # output all attentions, won't return them if self.output_attentions is False
        self.encoder = BertEncoder(bert_config, output_attentions=True,hidden_dim=self.hidden_dims)
        self.classifier = nn.Linear(self.hidden_size, num_classes)
        # self.pixelizer = nn.Linear(self.hidden_size, 3)
        self.register_buffer("attention_mask", torch.tensor(1.0))
        # self.pos_embedding = nn.Parameter(torch.randn(1, 8 , 8,self.hidden_size))
        # self.guided = SelfGuidedFilter(3)

        # self.mask_embedding = Parameter(torch.zeros(self.hidden_size))
        # self.cls_embedding = Parameter(torch.zeros(self.hidden_size))
        # self.reset_parameters()

    def reset_parameters(self):
        # self.mask_embedding.data.normal_(mean=0.0, std=0.01)
        # self.cls_embedding.data.normal_(mean=0.0, std=0.01)  # TODO no hard coded
        # self.positional_encoding.reset_parameters()
        pass

    def random_masking(self, batch_images, batch_mask, device):
        """
        with probability 10% we keep the image unchanged;
        with probability 10% we change the mask region to a normal distribution
        with 80% we mask the region as 0.
        :param batch_images: image to be masked
        :param batch_mask: mask region
        :param device:
        :return: masked image
        """
        return batch_images
        # TODO disabled
        temp = random.random()
        if temp > 0.1:
            batch_images = batch_images * batch_mask.unsqueeze(1).float()
            if temp < 0.2:
                batch_images = batch_images + (
                    ((-batch_mask.unsqueeze(1).float()) + 1)
                    * torch.normal(mean=0.5, std=torch.ones(batch_images.shape)).to(device)
                )
        return batch_images

    def prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    def reset_heads(self, heads_to_reset):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
        """
        for layer, heads in heads_to_reset.items():
            self.encoder.layer[layer].attention.reset_heads(heads)

    def forward(self, batch_images, batch_mask=None, feature_mask=None):

        """
        Replace masked pixels with 0s
        If ResNet
        | compute features
        | downscale the mask
        Replace masked pixels/features by MSK token
        Use Bert encoder
        """
        device = batch_images.device

        # compute ResNet features
        if self.with_resnet:

            # replace masked pixels with 0, batch_images has NCHW format
            batch_features_unmasked = self.extract_feature(batch_images)

            if batch_mask is not None:
                batch_images = self.random_masking(batch_images, batch_mask, device)
                batch_features = self.extract_feature(batch_images)
            else:
                batch_features = batch_features_unmasked

            # downscale the mask
            if batch_mask is not None:
                # downsample the mask
                # mask any downsampled pixel if it contained one masked pixel originialy
                feature_mask = ~(
                    F.max_pool2d((~batch_mask).float(), self.feature_downscale_factor).byte()
                )
            # reshape from NCHW to NHWC
            batch_features = batch_features.permute(0, 2, 3, 1)

        elif self.pooling_concatenate_size > 1:

            def downsample_concatenate(X, kernel):
                """X is of shape B x H x W x C
                return shape B x (kernel*H) x (kernel*W) x (kernel*kernel*C)
                """
                b, h, w, c = X.shape
                Y = X.contiguous().view(b, h, w // kernel, c * kernel)
                Y = Y.permute(0, 2, 1, 3).contiguous()
                Y = Y.view(b, w // kernel, h // kernel, kernel * kernel * c).contiguous()
                Y = Y.permute(0, 2, 1, 3).contiguous()
                return Y
            
            # reshape from NCHW to NHWC
            batch_features = batch_images.permute(0, 2, 3, 1)
            batch_features = downsample_concatenate(batch_features, self.pooling_concatenate_size)
            feature_mask = None
            if batch_mask is not None:
                feature_mask = batch_mask[
                    :, :: self.pooling_concatenate_size, :: self.pooling_concatenate_size
                ]

        else:
            batch_features = batch_images
            feature_mask = batch_mask            
            # reshape from NCHW to NHWC
            batch_features = batch_features.permute(0, 2, 3, 1)

        # feature upscale to BERT dimension
        batch_features = self.features_upscale(batch_features)
        # batch_features += self.pos_embedding

        b, w, h, _ = batch_features.shape

        all_attentions, all_representations = self.encoder(
            batch_features,
            attention_mask=self.attention_mask,
            output_all_encoded_layers=False,
        )

        representations = all_representations[0]

        # mean pool for representation (features for classification)
        cls_representation = representations.view(b, -1, representations.shape[-1]).mean(dim=1)
        cls_prediction = self.classifier(cls_representation)

        if self.output_attentions:
            return cls_prediction, all_attentions
        else:
            return cls_prediction
