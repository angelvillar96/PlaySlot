"""
Training and Validation of the Object-Centric Predictor (cOCVP) and
Inverse-Dynamics (InvDyn) modules from PlaySlot.
This training requires a frozen and pretrained SAVI video decomposition model.
"""

import argparse
import torch
# from sklearn.manifold import TSNE

from base.basePredictorTrainer import BasePredictorTrainer
from data.load_data import unwrap_batch_data
from lib.logger import Logger, print_
import lib.utils as utils
import lib.visualizations as visualizations

# hack to avoid weird port error in cluster
import multiprocessing
import multiprocessing.util
multiprocessing.util.abstract_sockets_supported = False
mgr = multiprocessing.Manager()



class Trainer(BasePredictorTrainer):
    """
    Training and Validation of the Object-Centric Predictor (cOCVP) and
    Inverse-Dynamics (InvDyn) modules from PlaySlot.
    This training requires a frozen and pretrained SAVI video decomposition model.
    """

    def forward_loss_metric(self, batch_data, training=False, inference_only=False, **kwargs):
        """
        Computing a forwad pass through the model, and computing the loss values and metrics

        Args:
        -----
        batch_data: dict
            Dictionary containing the information for the current batch,
            including images, actions, or metadata, among others.
        training: bool
            If True, model is in training mode
        inference_only: bool
            If True, only forward pass through the model is performed

        Returns:
        --------
        pred_data: dict
            Predictions from the model for the current batch of data
        loss: torch.Tensor
            Total loss for the current batch
        """
        num_slots = self.savi.num_slots
        slot_dim = self.savi.slot_dim

        # fetching and checking data
        videos, _, initializer_kwargs, _ = unwrap_batch_data(self.exp_params, batch_data)
        videos = videos.to(self.device)
        B, _, C, H, W = videos.shape
        num_context = self.exp_params["prediction_params"]["num_context"]
        num_preds = self.exp_params["prediction_params"]["num_preds"]

        # encoding frames into object slots usign pretrained SAVi
        with torch.no_grad():
            out_model = self.savi(
                    videos,
                    num_imgs=num_context + num_preds,
                    decode=False,
                    **initializer_kwargs
                )
            slot_history = out_model["slot_history"]

        # predicting future slots
        pred_slots, pred_others = self.predictor(
                imgs=videos,
                slots=slot_history,
                num_seed=num_context,
                num_preds=num_preds
            )

        # rendering future objects and frames from predicted object slots
        T = num_context  + num_preds - 1
        pred_slots_dec = pred_slots.clone().reshape(B * T, num_slots, slot_dim)
        img_recons, (pred_recons, pred_masks) = self.savi.decode(pred_slots_dec)
        pred_imgs = img_recons.view(B, T, C, H, W)

        # Generating only model outputs
        out_model = {
                "pred_imgs": pred_imgs,
                "pred_objs": pred_recons,
                "pred_masks": pred_masks,
                **pred_others
            }        
        if inference_only:
            return out_model, None

        # computing loss on all slots and images, including those from the context
        # NOTE: It is important to compute the loss in all so that we can later learn 
        #       behaviors starting with a single seed frame!
        pred_imgs = pred_imgs[:, :num_context+num_preds-1]
        pred_slots = pred_slots[:, :num_context+num_preds-1]
        target_slots = slot_history[:, 1:num_context + num_preds, :, :]
        target_imgs = videos[:, 1:num_context + num_preds, :, :]

        self.loss_tracker(
                preds=pred_slots,
                targets=target_slots,
                pred_imgs=pred_imgs,
                target_imgs=target_imgs,
                action_directions_dist=pred_others.pop("action_dist", None),
                **pred_others.pop("vq_losses", {}),
                **pred_others
            )
        loss = self.loss_tracker.get_last_losses(total_only=True)
        if training:
            self.optimization(loss=loss, iter_=self.iter_, epoch=self.epoch )
        return out_model, loss


    @torch.no_grad()
    def inference(self, imgs, init_kwargs, use_posterior=False, num_context=None, num_preds=None):
        """
        Running inference through the model for video prediction.
        This function is used for logging visualizations to the tensorboard.
        """
        # overriding 'num_context' or 'num_preds'
        if num_context is None or num_preds is None:
            num_context = self.exp_params["prediction_params"]["num_context"]
            num_preds = self.exp_params["prediction_params"]["num_preds"]

        # encoding images ino slots
        out_model = self.savi(
                imgs,
                num_imgs=num_context + num_preds,
                decode=False,
                **init_kwargs
            )
        slot_history = out_model["slot_history"]
        B, _, num_slots, slot_dim = slot_history.shape

        # computing latent actions using Inverse-Dynamics module
        if use_posterior:
            latent_action_model = self.predictor.latent_action
            out_dict = latent_action_model.compute_actions(slot_history)
            action_protos = out_dict.get("action_proto")
            action_vars = out_dict.get("action_variability", None)
            if len(action_protos.shape) == 3:  # inflating actions in InvDynS
                action_protos = action_protos.unsqueeze(2).repeat(1, 1, num_slots, 1)
                if action_vars is not None:
                    action_vars = action_vars.unsqueeze(2).repeat(1, 1, num_slots, 1)
        else:
            action_protos = None
            action_vars = None

        # autoregressively predicting next tokes
        pred_slots, _ = self.predictor.autoregressive_inference(
                slot_history[:, :num_context],
                action_protos=action_protos,
                action_vars=action_vars,
                N=num_preds
            )

        # rendering future objects and frames from predicted object slots
        pred_slots_dec = pred_slots.clone().reshape(B * num_preds, num_slots, slot_dim)
        img_recons, (pred_recons, pred_masks) = self.savi.decode(pred_slots_dec)
        pred_imgs = img_recons.view(B, num_preds, *imgs.shape[-3:])
        
        out_model = {
            "pred_imgs": pred_imgs,
            "pred_objs": pred_recons,
            "pred_masks": pred_masks,
        }
        return out_model


    @torch.no_grad()
    def visualizations(self, batch_data, epoch, iter_):
        """
        Making a visualization of some ground-truth, targets and predictions
        from the current model.
        """
        log_frequency = self.exp_params["training"]["image_log_frequency"]
        if(iter_ % log_frequency!= 0):
            return

        # predition visualizations
        num_context = self.exp_params["prediction_params"]["num_context"]
        num_preds = self.exp_params["prediction_params"]["num_preds"]
        imgs, _, init_kwargs, _ = unwrap_batch_data(self.exp_params, batch_data)
        imgs = imgs.to(self.device)
        self.visualizations_inference(
                imgs=imgs,
                init_kwargs=init_kwargs,
                num_context=num_context,
                num_preds=num_preds,
                iter_=iter_
            )        
        return


    def visualizations_inference(self, imgs, init_kwargs, num_context, num_preds, iter_):
        """ 
        Visualizing the results of an inference run
        """
        # inference with posterior and three random samples from the prior
        out_post = self.inference(imgs, init_kwargs, use_posterior=True)
        out_prior = [
                self.inference(imgs, init_kwargs, use_posterior=False)["pred_imgs"]
                for _ in range(3)
            ]
        all_preds = torch.stack([out_post["pred_imgs"]] + out_prior, dim=1)
        all_preds = all_preds.clamp(0, 1)
        imgs = imgs.clamp(0, 1)

        seed_imgs = imgs[:, :num_context, :, :]
        target_imgs = imgs[:, num_context:num_context+num_preds, :, :]

        # Visualizing pairwise-distance between codewords and histogram of their usage
        if hasattr(self.predictor.latent_action, "quantizer"):
            # visualizing pairwise distance between codewords
            codewords = self.predictor.latent_action.quantizer.get_codewords()
            fig, _ = visualizations.visualize_distance_between_centroids(codewords)
            self.writer.add_figure(tag=f"Centroid Distance", figure=fig, step=iter_)                
            # visualizing codeword counts
            action_bins = self.predictor.latent_action.quantizer.get_hist()
            _ = visualizations.visualize_bins_count(
                    bins=action_bins,
                    tb_writer=self.writer,
                    iter=iter_,
                    tag=f"Action_Bins"
                )         

        # prediction visualitations
        N = min(3, imgs.shape[0])
        ids = torch.arange(0, N)
        for idx in range(N):
            k = ids[idx]
            # posterior prediction
            fig, _ = visualizations.visualize_qualitative_eval(
                context=seed_imgs[k],
                targets=target_imgs[k],
                preds=all_preds[k, 0],  # posterior
                savepath=None
            )
            self.writer.add_figure(tag=f"Qualitative Eval {k+1}", figure=fig, step=iter_)

            # stochastic predicitons
            fig, _ = visualizations.visualize_stoch_frame_figs(
                context=seed_imgs[k],
                targets=target_imgs[k],
                all_preds=all_preds[k],
                titles=["Posterior Preds"] + [f"Random Preds {i+1}" for i in range(3)]
            )
            self.writer.add_figure(tag=f"Stochastic Eval {k+1}", figure=fig, step=iter_)

            # predicted objects and alpha masks
            pred_objs = out_post["pred_objs"][k*num_preds:(k+1)*num_preds]
            pred_masks = out_post["pred_masks"][k*num_preds:(k+1)*num_preds]
            _ = visualizations.visualize_decomp(
                    (pred_objs * pred_masks).clamp(0, 1),
                    savepath=None,
                    tag=f"Pred. Object Recons. {k+1}",
                    tb_writer=self.writer,
                    iter=iter_
                )
        return


if __name__ == "__main__":
    utils.clear_cmd()
    
    # process command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
            "-d", "--exp_directory",
            help="Path to the SAVi exp. directory. It includes the predictor exp.",
            required=True
        )
    parser.add_argument(
            "--savi_ckpt",
            help="Name of the pretrained SAVi checkpoint to use",
            required=True
        )
    parser.add_argument(
            "--name_pred_exp",
            help="Name to the predictor experiment to train.",
            required=True
        )
    # for resuming training
    parser.add_argument(
            "--checkpoint",
            help="Checkpoint with predictor pretrained parameters to load",
            default=None
        )
    parser.add_argument(
            "--resume_training",
            help="Resuming training",
            default=False,
            action='store_true'
        )
    args = parser.parse_args()
    
    # sanity checks on arguments
    exp_path = utils.process_experiment_directory_argument(args.exp_directory)
    savi_ckpt = utils.process_checkpoint_argument(exp_path, args.savi_ckpt)
    args.name_pred_exp = utils.process_predictor_experiment(
            exp_directory=exp_path,
            name_predictor_experiment=args.name_pred_exp,    
        )
    args.checkpoint = utils.process_predictor_checkpoint(
            exp_path=exp_path,
            name_predictor_experiment=args.name_pred_exp,
            checkpoint=args.checkpoint
        )
    
    # Trainer and so on
    logger = Logger(exp_path=f"{exp_path}/{args.name_pred_exp}")
    logger.log_info(
            "Starting PlaySlot Predictor and InvDyn training procedure",
            message_type="new_exp"
        )
    logger.log_arguments(args)

    print_("Initializing PlaySlot Predictor and InvDyn Trainer...")
    trainer = Trainer(
            name_pred_exp=args.name_pred_exp,
            exp_path=exp_path,
            savi_ckpt=args.savi_ckpt,
            checkpoint=args.checkpoint,
            resume_training=args.resume_training
        )
    print_("Loading dataset...")
    trainer.load_data()
    print_("Setting up model, predictor and optimizer")
    trainer.load_savi()
    trainer.setup_predictor()
    print_("Starting to train")
    trainer.training_loop()


#
