# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

## Modified by Shreya to add Factual and Generative reward models

# DeepSpeed Team
import time
import torch
import deepspeed
from deepspeed.ops.adam import FusedAdam
from deepspeed.ops.adam import DeepSpeedCPUAdam
from transformers import AutoModelForCausalLM, get_scheduler

from dschat.utils.ds_utils import get_train_ds_config, get_eval_ds_config
from dschat.utils.module.lora import convert_linear_layer_to_lora, only_optimize_lora_parameters, make_model_gradient_checkpointing_compatible
from dschat.utils.model.model_utils import create_hf_model, create_critic_model
from dschat.utils.utils import get_optimizer_grouped_parameters
"""
TODOs:
  * support HF models for critic (for debugging), must be a previously saved ckpt from step-2
  * determine ds_config/zero_stage based on model size, gpu style, world size, etc
    - get model size by creating simple meta model
    - 1.3b: zero-2 for actor/ref models, zero-0 for others
    - 13b+: zero-3 for all models
"""


def log_init(model_name, stime=None):
    if torch.distributed.get_rank() == 0:
        tag = "start" if stime is None else "end"
        suffix = "ing" if stime is None else "ed"
        duration = ""
        if stime is not None:
            duration = "(duration: {:.2f}s)".format(time.time() - stime)
        msg = f"[{tag}] Initializ{suffix} {model_name} Model [{tag}] {duration}"
        stars = (90 - len(msg)) // 2
        extra_star = "*" if (90 - len(msg)) % 2 == 1 else ""
        print("*" * stars + msg + "*" * stars + extra_star)
        return time.time()


class DeepSpeedRLHFEngine():

    def __init__(self, actor_model_name_or_path, fact_critic_model_name_or_path, gen_critic_model_name_or_path, 
                 tokenizer, args, num_total_iters):
        self.args = args
        self.num_total_iters = num_total_iters
        self.tokenizer = tokenizer

        self.actor = self._init_actor(
            actor_model_name_or_path=actor_model_name_or_path)
        self.ref = self._init_ref(
            actor_model_name_or_path=actor_model_name_or_path)
        self.actor_ema = None
        if self.args.enable_ema:
            self.actor_ema = self._init_ema(
                actor_model_name_or_path=actor_model_name_or_path)
        self.critic_fact, self.critic_gen = self._init_critic(
            fact_critic_model_name_or_path=fact_critic_model_name_or_path, gen_critic_model_name_or_path=gen_critic_model_name_or_path)
        # self.critic_gen = self._init_critic(
            
        self.reward_fact, self.reward_gen = self._init_reward(
            fact_critic_model_name_or_path=fact_critic_model_name_or_path, gen_critic_model_name_or_path=gen_critic_model_name_or_path)
        # self.reward_gen = self._init_reward(
            
        if self.args.critic_gradient_checkpointing:
            self.critic_fact.gradient_checkpointing_enable()
            self.critic_gen.gradient_checkpointing_enable()
            

    def _init_actor(self, actor_model_name_or_path):
        stime = log_init("Actor")

        # DS Config
        ds_config = get_train_ds_config(
            offload=self.args.offload,
            dtype=self.args.dtype,
            stage=self.args.actor_zero_stage,
            enable_hybrid_engine=self.args.enable_hybrid_engine,
            inference_tp_size=self.args.inference_tp_size,
            release_inference_cache=self.args.release_inference_cache,
            pin_parameters=(not self.args.unpin_actor_parameters),
            tp_gather_partition_size=self.args.tp_gather_partition_size,
            max_out_tokens=self.args.max_prompt_seq_len +
            self.args.max_answer_seq_len,
            enable_tensorboard=self.args.enable_tensorboard,
            enable_mixed_precision_lora=self.args.enable_mixed_precision_lora,
            tb_path=self.args.tensorboard_path,
            tb_name="step3_actor")
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        #TODO(jeff): we should probably set grad accumlation steps here as well for clarity
        ds_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps_actor

        # Model
        actor_model = create_hf_model(
            model_class=AutoModelForCausalLM,
            model_name_or_path=actor_model_name_or_path,
            tokenizer=self.tokenizer,
            ds_config=ds_config,
            dropout=self.args.actor_dropout)

        # LoRA
        if self.args.actor_lora_dim > 0:
            actor_model = convert_linear_layer_to_lora(
                actor_model, self.args.actor_lora_module_name,
                self.args.actor_lora_dim)
            if self.args.only_optimize_lora:
                actor_model = only_optimize_lora_parameters(actor_model)
                actor_model = make_model_gradient_checkpointing_compatible(
                    actor_model)

        # Optimizer
        AdamOptimizer = DeepSpeedCPUAdam if self.args.offload else FusedAdam
        optim_params = get_optimizer_grouped_parameters(
            actor_model, self.args.actor_weight_decay,
            self.args.actor_lora_learning_rate)
        optim = AdamOptimizer(optim_params,
                              lr=self.args.actor_learning_rate,
                              betas=(0.9, 0.95))

        # LR Scheduler
        lr_scheduler = get_scheduler(
            name=self.args.lr_scheduler_type,
            optimizer=optim,
            num_warmup_steps=self.args.num_warmup_steps,
            num_training_steps=self.num_total_iters,
        )

        # DeepSpeed Engine
        #TODO: move enable_hybrid_engine and pin_parameters to ds_config
        actor_engine, *_ = deepspeed.initialize(model=actor_model,
                                                optimizer=optim,
                                                lr_scheduler=lr_scheduler,
                                                config=ds_config)

        log_init("Actor", stime=stime)

        return actor_engine

    def _init_ref(self, actor_model_name_or_path):
        stime = log_init("Ref")
        # DS Config
        zero_stage = self.args.actor_zero_stage
        if zero_stage != 3:
            # If actor is ZeRO-3 then we use it for everything, otherwise assume we have enough memory for ref model
            zero_stage = 0
        ds_config = get_eval_ds_config(self.args.offload_reference_model,
                                       self.args.dtype, zero_stage)
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        #TODO(jeff): we should probably set grad accumlation steps here as well for clarity
        ds_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps_actor

        ref_model = create_hf_model(AutoModelForCausalLM,
                                    actor_model_name_or_path, self.tokenizer,
                                    ds_config)

        ref_engine, *_ = deepspeed.initialize(model=ref_model,
                                              config=ds_config)

        log_init("Ref", stime=stime)
        return ref_engine

    def _init_ema(self, actor_model_name_or_path):
        stime = log_init("EMA")
        # DS Config
        zero_stage = self.args.actor_zero_stage
        if zero_stage != 3:
            # If actor is ZeRO-3 then we use it for everything, otherwise assume we have enough memory
            zero_stage = 0
        ds_config = get_eval_ds_config(self.args.offload_reference_model,
                                       self.args.dtype, zero_stage)
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        #TODO(jeff): we should probably set grad accumlation steps here as well for clarity
        ds_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps_actor

        actor_model_ema = create_hf_model(AutoModelForCausalLM,
                                          actor_model_name_or_path,
                                          self.tokenizer, ds_config)
        if self.args.actor_lora_dim > 0:
            actor_model_ema = convert_linear_layer_to_lora(
                actor_model_ema, self.args.actor_lora_module_name,
                self.args.actor_lora_dim)

        ema_engine, *_ = deepspeed.initialize(model=actor_model_ema,
                                              config=ds_config)

        log_init("EMA", stime=stime)
        return ema_engine

    def _init_critic(self, fact_critic_model_name_or_path, gen_critic_model_name_or_path):
        stime = log_init("Critic")
        ds_config = get_train_ds_config(
            offload=self.args.offload,
            dtype=self.args.dtype,
            stage=self.args.critic_zero_stage,
            enable_tensorboard=self.args.enable_tensorboard,
            tb_path=self.args.tensorboard_path,
            tb_name="step3_critic")
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        #TODO(jeff): we should probably set grad accumlation steps here as well for clarity
        ds_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps

        ds_eval_config = get_eval_ds_config(offload=False,
                                            dtype=self.args.dtype,
                                            stage=self.args.critic_zero_stage)
        # We need to set train batch size and micro batch size here to pass the sanity check of DeepSpeed engine.
        ds_eval_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        ds_eval_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps

        # Model
        fact_critic_model = create_critic_model(
            model_name_or_path=fact_critic_model_name_or_path,
            tokenizer=self.tokenizer,
            ds_config=ds_eval_config,
            num_padding_at_beginning=self.args.num_padding_at_beginning,
            rlhf_training=True,
            dropout=self.args.critic_dropout,
            zero_stage=self.args.critic_zero_stage)
        
        gen_critic_model = create_critic_model(
            model_name_or_path=gen_critic_model_name_or_path,
            tokenizer=self.tokenizer,
            ds_config=ds_eval_config,
            num_padding_at_beginning=self.args.num_padding_at_beginning,
            rlhf_training=True,
            dropout=self.args.critic_dropout,
            zero_stage=self.args.critic_zero_stage)

        # LoRA
        if self.args.critic_lora_dim > 0:
            fact_critic_model = convert_linear_layer_to_lora(
                fact_critic_model, self.args.critic_lora_module_name,
                self.args.critic_lora_dim)
            if self.args.only_optimize_lora:
                fact_critic_model = only_optimize_lora_parameters(fact_critic_model)
                fact_critic_model = make_model_gradient_checkpointing_compatible(
                    fact_critic_model)
                
            gen_critic_model = convert_linear_layer_to_lora(
                gen_critic_model, self.args.critic_lora_module_name,
                self.args.critic_lora_dim)
            if self.args.only_optimize_lora:
                gen_critic_model = only_optimize_lora_parameters(gen_critic_model)
                gen_critic_model = make_model_gradient_checkpointing_compatible(
                    gen_critic_model)

        # Optimizer
        AdamOptimizer = DeepSpeedCPUAdam if self.args.offload else FusedAdam
        fact_optim_params = get_optimizer_grouped_parameters(
            fact_critic_model, self.args.critic_weight_decay,
            self.args.critic_lora_learning_rate)
        gen_optim_params = get_optimizer_grouped_parameters(
            gen_critic_model, self.args.critic_weight_decay,
            self.args.critic_lora_learning_rate)
        fact_optim = AdamOptimizer(fact_optim_params,
                              lr=self.args.critic_learning_rate,
                              betas=(0.9, 0.95))
        gen_optim = AdamOptimizer(gen_optim_params,
                              lr=self.args.critic_learning_rate,
                              betas=(0.9, 0.95))

        # LR Scheduler
        fact_lr_scheduler = get_scheduler(
            name=self.args.lr_scheduler_type,
            optimizer=fact_optim,
            num_warmup_steps=self.args.num_warmup_steps,
            num_training_steps=self.num_total_iters,
        )

        gen_lr_scheduler = get_scheduler(
            name=self.args.lr_scheduler_type,
            optimizer=gen_optim,
            num_warmup_steps=self.args.num_warmup_steps,
            num_training_steps=self.num_total_iters,
        )

        # DeepSpeed Engine
        fact_critic_engine, *_ = deepspeed.initialize(model=fact_critic_model,
                                                 optimizer=fact_optim,
                                                 lr_scheduler=fact_lr_scheduler,
                                                 config=ds_config)
        
        gen_critic_engine, *_ = deepspeed.initialize(model=gen_critic_model,
                                                 optimizer=gen_optim,
                                                 lr_scheduler=gen_lr_scheduler,
                                                 config=ds_config)

        log_init("Critic", stime=stime)
        return fact_critic_engine, gen_critic_engine

    def _init_reward(self, fact_critic_model_name_or_path, gen_critic_model_name_or_path):
        stime = log_init("Reward")
        # DS Config
        zero_stage = self.args.critic_zero_stage
        if zero_stage != 3:
            # If critic is ZeRO-3 then we use it for everything, otherwise assume we have enough memory
            zero_stage = 0

        ds_config = get_eval_ds_config(offload=self.args.offload,
                                       dtype=self.args.dtype,
                                       stage=zero_stage)
        ds_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        ds_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps

        ds_eval_config = get_eval_ds_config(offload=False,
                                            dtype=self.args.dtype,
                                            stage=zero_stage)

        # We need to set train batch size and micro batch size here to pass the sanity check of DeepSpeed engine.
        ds_eval_config[
            'train_micro_batch_size_per_gpu'] = self.args.per_device_training_batch_size
        ds_eval_config[
            'train_batch_size'] = self.args.per_device_training_batch_size * torch.distributed.get_world_size(
            ) * self.args.gradient_accumulation_steps

        # Model
        fact_reward_model = create_critic_model(
            model_name_or_path=fact_critic_model_name_or_path,
            tokenizer=self.tokenizer,
            ds_config=ds_eval_config,
            num_padding_at_beginning=self.args.num_padding_at_beginning,
            rlhf_training=True,
            dropout=self.args.critic_dropout,
            zero_stage=zero_stage)
        
        gen_reward_model = create_critic_model(
            model_name_or_path=gen_critic_model_name_or_path,
            tokenizer=self.tokenizer,
            ds_config=ds_eval_config,
            num_padding_at_beginning=self.args.num_padding_at_beginning,
            rlhf_training=True,
            dropout=self.args.critic_dropout,
            zero_stage=zero_stage)

        fact_reward_engine, *_ = deepspeed.initialize(model=fact_reward_model,
                                                 config=ds_config)
        
        gen_reward_engine, *_ = deepspeed.initialize(model=gen_reward_model,
                                                 config=ds_config)

        log_init("Reward", stime=stime)
        return fact_reward_engine, gen_reward_engine
