import argparse
import torch
import wandb
from peft import LoraConfig, PeftModel
from alignment import DPOConfig
from trl import DPOTrainer
from src.emp_metrics.ed_load import get_ed_for_dpo
from huggingface_hub import login
from pathlib import Path
from transformers import AutoTokenizer, BitsAndBytesConfig, AutoModelForCausalLM
from numpy import percentile
import os
import re
import shutil
import glob
import time

def train_dpo(base_model_id, model_id, output_dir_base, new_name):
    output_dir = output_dir_base + model_id
    
    dpo_output_dir = output_dir_base + model_id + "_" + new_name
    an = model_id + '_' + new_name
    # import ipdb; ipdb.set_trace()
    prev_ords = [
            match.group().replace(an, "") 
            for d in os.listdir(output_dir_base) 
            if (match := re.search(rf"{an}\d+$", d))]
    prev_ords = [int(x.replace(an, "")) for x in prev_ords]
    # import ipdb; ipdb.set_trace()
    dpo_output_dir = dpo_output_dir + str(
                 1 + max(prev_ords) if len(prev_ords) > 0 else 0) 

    print(dpo_output_dir)
    config = wandb.config
    tokenizer = AutoTokenizer.from_pretrained(output_dir)

    # load datasets
    sys_msg = "You are a friendly assistant, who provides empathetic responses to the user. " \
              "The input contains previous turn of the dialog, where the each utterance is prefaced " \
              "with tags <|user|>, or <|assistant|>. Be empathetic and precise. Make sure to give " \
              "responses that make dialogue flow. Avoid repeating the prompt."

    train_dataset = get_ed_for_dpo("train", tokenizer, sys_msg=sys_msg,
                                   tokenize=False, add_generation_prompt=True)
    eval_dataset = get_ed_for_dpo("test", tokenizer, sys_msg=sys_msg,
                                  tokenize=False, add_generation_prompt=True)

    # find the p95 length of the prompt
    prompt_length = int(percentile(
        [len(tokenizer(x)["input_ids"]) for x in train_dataset["prompt"]], 95))
    max_seq_length_chosen = int(percentile([len(tokenizer(x["prompt"] + x["chosen"])["input_ids"]) for x in train_dataset], 95))
    max_seq_length_rejected = int(percentile([len(tokenizer(x["prompt"] + x["rejected"])["input_ids"]) for x in train_dataset], 95))
    max_seq_length = max(max_seq_length_chosen, max_seq_length_rejected)

    if config.test_frac < 0.9999:
        train_dataset = train_dataset.select(
                        range(int(len(train_dataset) * config.test_frac)))
        eval_dataset = eval_dataset.select(
                      range(int(len(eval_dataset) * config.test_frac)))

    # filter datasets to remove samples that are too long
    train_dataset = train_dataset.filter(lambda x: len(tokenizer(x["prompt"] + x["chosen"])["input_ids"]) <= max_seq_length)
    eval_dataset = eval_dataset.filter(lambda x: len(tokenizer(x["prompt"] + x["chosen"])["input_ids"]) <= max_seq_length)

    # Up the lengths to next multiple of 2
    prompt_length = ((prompt_length + 1) // 2) * 2
    max_seq_length = ((max_seq_length + 1) // 2) * 2

    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        llm_int8_threshold=6.0,
        llm_int8_has_fp16_weight=False,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
    )

    model = AutoModelForCausalLM.from_pretrained(
        base_model_id,
        quantization_config=bnb_config,
        attn_implementation="flash_attention_2",
        torch_dtype=torch.bfloat16,
        device_map="auto",
        use_cache=False
    )
    model.resize_token_embeddings(len(tokenizer))
    model = PeftModel.from_pretrained(model, output_dir, is_trainable=True)

    # Load the adapter a second time, with a different name, which will be 
    # our reference model.
    #   -> doesnt work, load the model twice
    # model.load_adapter(output_dir, adapter_name="reference")

    ref_model = AutoModelForCausalLM.from_pretrained(
        base_model_id,
        # load_in_4bit=True,
        quantization_config=bnb_config,
        attn_implementation="flash_attention_2",
        torch_dtype=torch.bfloat16,
        device_map="auto",
        use_cache=False
    )
    ref_model.resize_token_embeddings(len(tokenizer))
    ref_model = PeftModel.from_pretrained(ref_model, output_dir, 
                                          is_trainable=False)
    print("------------Both adapters loaded--------------")
    training_args = DPOConfig(
        output_dir=dpo_output_dir,
        num_train_epochs=1,
        per_device_train_batch_size=config.dpo_per_device_train_batch_size,
        per_device_eval_batch_size=config.dpo_per_device_eval_batch_size,
        gradient_accumulation_steps=1,
        gradient_checkpointing=True,
        optim="adamw_torch_fused",
        learning_rate=config.dpo_learning_rate,
        max_grad_norm=0.3,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine",
        logging_steps=0.1,
        save_steps=0.1,
        save_total_limit=2,
        evaluation_strategy="steps",
        eval_steps=0.2,
        bf16=True,
        # tf32=True,
        push_to_hub=False,
        report_to="wandb"
    )
    dpo_args = {
        "beta": config.dpo_beta,  # Higher beta means less divergence
        "loss_type": "sigmoid"
    }
    trainer = DPOTrainer(
                model,
                ref_model,
                # model_adapter_name="train2", -> doesnt work
                # ref_adapter_name="reference", -> doesnt work
                args=training_args,
                train_dataset=train_dataset,
                eval_dataset=eval_dataset,
                tokenizer=tokenizer,
                max_length=max_seq_length,
                max_prompt_length=prompt_length,
                beta=dpo_args["beta"],
                loss_type=dpo_args["loss_type"],
                model_init_kwargs=None,
                ref_model_init_kwargs=None
            )

    trainer.train()
    trainer.save_model(dpo_output_dir)
    print(f"5.-----Saving DPO to: {dpo_output_dir}--------")
    del model
    del ref_model
    del tokenizer
    del trainer
    checkpt_dirs = glob.glob(dpo_output_dir + "/checkpoint-*")
    for dir_path in checkpt_dirs:
        shutil.rmtree(dir_path)
    time.sleep(5)

    return dpo_output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-g", "--gpu", default="1", help="not implemented")
    parser.add_argument("-bm", "--base_model",
                        default="alignment-handbook/zephyr-7b-sft-lora",
                        help="base model name")
    parser.add_argument("-a", "--adapter", help="adapter name")
    parser.add_argument("-d", "--base_dir", default="./results/",
                        help="base dir with saved models")
    parser.add_argument("-n", "--new_name", help="save name")

    ARGS = parser.parse_args()
    train_dpo(ARGS.base_model, ARGS.adapter, ARGS.base_dir, ARGS.new_name)
