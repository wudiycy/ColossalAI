#!/bin/bash

################
#Load your environments and modules here
################

HOSTFILE=$(realpath hosts.txt)

cd ../..

torchrun --standalone --nproc_per_node 8 finetune.py \
    --plugin "hybrid_parallel" \
    --dataset "yizhongw/self_instruct" \
    --model_path "/path/llama" \
    --task_name "super_natural_instructions" \
    --save_dir "/path/output"
