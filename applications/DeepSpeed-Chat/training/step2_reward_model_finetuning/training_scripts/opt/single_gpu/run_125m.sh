#!/bin/bash
# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
OUTPUT=$1
ZERO_STAGE=$2
if [ "$OUTPUT" == "" ]; then
    OUTPUT=./output
fi
if [ "$ZERO_STAGE" == "" ]; then
    ZERO_STAGE=0
fi
mkdir -p $OUTPUT

<<<<<<< HEAD
deepspeed --num_gpus 1 main.py --model_name_or_path "thegrey007/opt-125m-finetuned" --data_path "thegrey007/factual"\
   --num_padding_at_beginning 1 --weight_decay 0.1 --dropout 0.0 --gradient_accumulation_steps 4 --zero_stage $ZERO_STAGE --per_device_train_batch_size 24 --per_device_eval_batch_size 24\
=======
deepspeed --num_gpus 1 main.py --model_name_or_path facebook/opt-125m \
   --num_padding_at_beginning 1 --weight_decay 0.1 --dropout 0.0 --gradient_accumulation_steps 4 --zero_stage $ZERO_STAGE --per_device_train_batch_size 8 --per_device_eval_batch_size 8\
<<<<<<< HEAD
<<<<<<< HEAD
>>>>>>> d6e14d5814c5c3d9406f3db98b08d624192ea9aa
=======
>>>>>>> d6e14d5814c5c3d9406f3db98b08d624192ea9aa
=======
>>>>>>> d45cd2ed850b3fca9005126ff4a619a74a8a0999
   --enable_tensorboard \
   --tensorboard_path $OUTPUT \
   --deepspeed --output_dir $OUTPUT &> $OUTPUT/training.log
