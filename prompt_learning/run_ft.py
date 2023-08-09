from __future__ import absolute_import, division, print_function
import argparse
import json
import logging
import os
import random
import copy
import math
import numpy as np
import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset, SubsetRandomSampler
import sklearn.metrics as mtc
from scipy.stats import spearmanr
from tqdm import tqdm, trange
from transformers import AutoTokenizer, GPT2LMHeadModel
from transformers import SchedulerType, get_scheduler
#from peft import get_peft_model, LoraConfig, TaskType, PeftModel
from accelerate import Accelerator


logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
                    datefmt="%m/%d/%Y %H:%M:%S",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

'''
with open("data/database.json") as f:
    db = json.load(f)
'''

class InputExample(object):
    def __init__(self, guid, context=None, source=None, target=None):
        self.guid = guid
        self.context = context
        self.source = source
        self.target = target


class InputFeatures(object):
    def __init__(self, input_ids, attention_mask, labels):
        self.input_ids = input_ids
        self.attention_mask = attention_mask
        self.labels = labels

class EcspellProcessor:
    """Processor for the ECSpell data set."""

    def get_train_examples(self, data_dir, division="law"):
        return self._create_examples(self._read_csv(os.path.join(data_dir, "train_{}.txt".format(division))), "train")

    def get_dev_examples(self, data_dir, division="law"):
        return self._create_examples(self._read_csv(os.path.join(data_dir, "test_{}.txt".format(division))), "dev")

    def get_test_examples(self, data_dir, division="law"):
        return self._create_examples(self._read_csv(os.path.join(data_dir, "test_{}.txt".format(division))), "test")

    @staticmethod
    def _read_csv(input_file):
        with open(input_file, "r", encoding="utf-8") as f:
            lines = []
            for line in f:
                src, trg = line.strip().split("\t")
                lines.append((src.split(), trg.split()))
            return lines

    @staticmethod
    def _create_examples(lines, set_type):
        examples = []
        for i, (src, trg) in enumerate(lines):
            guid = "%s-%s" % (set_type, i)
            if len(src) == len(trg):
                if len(src) == len(trg):
                    examples.append(InputExample(guid=guid, source=src, target=trg))
        return examples
    

def convert_examples_to_features(examples, max_seq_length, tokenizer):
    features = []
    max_length=max_seq_length//2-2
    def truncate(x, max_length):
        return x[: max_length]
    for i, example in enumerate(examples):
        #truncate the source and the target
        example.source = truncate(example.source,max_length)
        example.target = truncate(example.target,max_length)

        encoded_inputs = tokenizer(example.source, add_special_tokens=True ,is_split_into_words=True)
        #encoded_inputs['input_ids']=[tokenizer.cls_token_id]+encoded_inputs['input_ids']
        encoded_inputs["labels"] = [-100] * len(encoded_inputs["input_ids"])

        trg_ids= tokenizer(example.target, add_special_tokens=False, is_split_into_words=True)["input_ids"] + [tokenizer.eos_token_id]
        encoded_inputs["input_ids"] += trg_ids
        encoded_inputs["labels"] += trg_ids
        encoded_inputs["attention_mask"] = [1] * len(encoded_inputs["input_ids"])
    
        offset_length = max_seq_length - len(encoded_inputs["input_ids"])
        # pad right
        encoded_inputs["input_ids"] = encoded_inputs["input_ids"] + [tokenizer.pad_token_id] * offset_length
        encoded_inputs["attention_mask"] = encoded_inputs["attention_mask"] + [0] * offset_length
        encoded_inputs["labels"] =  encoded_inputs["labels"] + [-100] * offset_length
        
        input_ids = encoded_inputs["input_ids"]
        attention_mask = encoded_inputs["attention_mask"]
        labels = encoded_inputs["labels"]
        tokens = tokenizer.convert_ids_to_tokens(input_ids)

        assert len(input_ids) == max_seq_length
        assert len(attention_mask) == max_seq_length
        assert len(labels) == max_seq_length

        if i < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % example.guid)
            logger.info("tokens: %s" % " ".join(tokens))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("attention_mask: %s" % " ".join([str(x) for x in attention_mask]))
            logger.info("labels: %s" % " ".join([str(x) for x in labels]))

        '''
        input_ids: cls + src + sep + trg + sep +pad
        labels:   ...-100...       + trg + sep +pad
        '''
        features.append(
            InputFeatures(input_ids=input_ids,
                          attention_mask=attention_mask,
                          labels=labels)
        )

    return features

class Metrics:
    @staticmethod
    def compute(src_sents, trg_sents, prd_sents,tokenizer=None):
        def difference(src, trg):
            ret = copy.deepcopy(src)
            for i, (src_char, trg_char) in enumerate(zip(src, trg)):
                if src_char!= trg_char:
                    ret[i] = "(" + src_char + "->" + trg_char + ")"

            return "".join(ret)
        def decode(x):
                        return tokenizer.convert_ids_to_tokens(x, skip_special_tokens=True)
        pos_sents, neg_sents, tp_sents, fp_sents, fn_sents, prd_pos_sents, prd_neg_sents, wp_sents = [], [], [], [], [], [], [], []
        for s, t, p in zip(src_sents, trg_sents, prd_sents):
            if tokenizer is not None:
                s = decode(s)
                t = decode(t)
                p = decode(p)
            # For positive examples
            if s != t:
                pos_sents.append(difference(s, t))
                if p == t:
                    tp_sents.append(difference(s, t))
                if p == s:
                    fn_sents.append(difference(s, t))
                if (p!=t and p!=s):
                    wp_sents.append(difference(s,t))
            # For negative examples
            else:
                neg_sents.append(difference(s, t))
                if p != t:
                    fp_sents.append(difference(t, p))
            # For predictions
            if s != p:
                prd_pos_sents.append(difference(s, p))
            if s == p:
                prd_neg_sents.append(difference(s, p))

        p = 1.0 * len(tp_sents) / len(prd_pos_sents)
        r = 1.0 * len(tp_sents) / len(pos_sents)
        f1 = 2.0 * (p * r) / (p + r + 1e-12)
        fpr = 1.0 * (len(fp_sents) + 1e-12) / (len(neg_sents) + 1e-12)

        return p, r, f1, fpr, tp_sents, fp_sents, fn_sents, wp_sents
    
def dynamic_mask_token(inputs, targets, tokenizer, device, noise_probability=0.2):
    #src:[CLS],x1,x2,...,xn,[SEP],y1,y2,y3 [SEP]
    #trg:-100 , ... -100, -100 ,  y1,y2,y3 [SEP]
    ## mask_mode in ["all","error","noerror"]
    inputs = inputs.clone()
    probability_matrix = torch.full(inputs.shape, noise_probability).to(device)
    #do not mask sepcail tokens
    special_tokens_mask = [
        tokenizer.get_special_tokens_mask(val, already_has_special_tokens=True) for val in inputs.tolist()
    ]
    special_tokens_mask = torch.tensor(special_tokens_mask, dtype=torch.bool).to(device)
    ## do not mask target part
    probability_matrix.masked_fill_(inputs==targets, value=0.0)
    
    masked_indices = torch.bernoulli(probability_matrix).bool()
    inputs[masked_indices] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)

    return inputs
def main():
    parser = argparse.ArgumentParser()

    # Data config
    parser.add_argument("--data_dir", type=str, default="data/",
                        help="Directory to contain the input data for all tasks.")
    parser.add_argument("--task_name", type=str, default="SIGHAN",
                        help="Name of the training task.")
    parser.add_argument("--load_model_path", type=str, default="uer/gpt2-chinese-cluecorpussmall",
                        help="Pre-trained language model to load.")
    parser.add_argument("--load_tokenizer_path", type=str, default="bert-base-uncased",
                        help="Pre-trained tokenizer to load.")
    parser.add_argument("--cache_dir", type=str, default="../cache/",
                        help="Directory to store the pre-trained language models downloaded from s3.")
    parser.add_argument("--output_dir", type=str, default="model/",
                        help="Directory to output predictions and checkpoints.")
    parser.add_argument("--load_state_dict", type=str, default="",
                        help="Checkpoint to load for trianing or evaluation.")

    # Training config
    parser.add_argument("--do_train", action="store_true",
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action="store_true",
                        help="Whether to evaluate on the dev set.")
    parser.add_argument("--do_test", action="store_true",
                        help="Whether to evaluate on the test set.")
    parser.add_argument("--train_on", type=str, default="",
                        help="Choose a training set.")
    parser.add_argument("--eval_on", type=str, default="",
                        help="Choose a dev set.")
    parser.add_argument("--do_lower_case", action="store_true",
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--max_seq_length", type=int, default=128,
                        help="Maximum total input sequence length after word-piece tokenization.")
    parser.add_argument("--train_batch_size", type=int, default=128,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", type=int, default=256,
                        help="Total batch size for evaluation.")
    parser.add_argument("--learning_rate", type=float, default=3e-5,
                        help="Peak learning rate for optimization.")
    parser.add_argument("--num_train_epochs", type=float, default=3.0,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_train_steps", type=int, default=None,
                        help="Total number of training steps to perform (overrides training epochs).")
    parser.add_argument("--lr_scheduler_type", type=SchedulerType, default="linear",
                        help="Scheduler type for learning rate warmup.")
    parser.add_argument("--warmup_proportion", type=float, default=0.06,
                        help="Proportion of training to perform learning rate warmup for.")
    parser.add_argument("--weight_decay", type=float, default=0.01,
                        help="L2 weight decay for training.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward pass.")
    parser.add_argument("--no_cuda", action="store_true",
                        help="Whether not to use CUDA when available.")
    parser.add_argument("--fp16", action="store_true",
                        help="Whether to use mixed precision.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for initialization.")
    parser.add_argument("--lora", action="store_true",
                        help="Whether to use low rank adaption.")
    parser.add_argument("--doask", action="store_true",
                        help="Whether to augment the training data.")
    parser.add_argument("--save_steps", type=int, default=100,
                        help="How many steps to save the checkpoint once.")
    
    parser.add_argument("--mft", action="store_true",
                        help="Training with masked-fine-tuning (not published yet).")
    parser.add_argument("--mask_mode", type=str, default="noerror", help="noerror,error or all")
    parser.add_argument("--mask_rate", type=float, default=0.2, help="the percentage we mask the source sentence in mask-ft technique")

    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, "-accelerate", args.fp16))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    if args.do_train:
        torch.save(args, os.path.join(args.output_dir, "train_args.bin"))

    processor = EcspellProcessor()

    cache_dir = args.cache_dir
    tokenizer = AutoTokenizer.from_pretrained(args.load_tokenizer_path,
                                              do_lower_case=args.do_lower_case,
                                              padding_side="left",
                                              cache_dir=cache_dir)
    if getattr(tokenizer, "pad_token_id") is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id
    # we use BERTtokenizer as tokenizer for chinese
    if getattr(tokenizer, "eos_token_id") is None:
        tokenizer.eos_token_id = tokenizer.sep_token_id

    logger.info("tokenizer.eos_token_id: %d", tokenizer.eos_token_id)
    task_name = args.task_name.lower()

    if args.do_train:
        train_examples = processor.get_train_examples(os.path.join(args.data_dir, task_name), args.train_on)
        train_features = convert_examples_to_features(train_examples, args.max_seq_length, tokenizer)

        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        all_attention_mask = torch.tensor([f.attention_mask for f in train_features], dtype=torch.long)
        all_labels = torch.tensor([f.labels for f in train_features], dtype=torch.long)

        train_data = TensorDataset(all_input_ids, all_attention_mask, all_labels)
        train_dataloader = DataLoader(train_data, shuffle=True, batch_size=args.train_batch_size)

        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        if args.max_train_steps is None:
            args.max_train_steps = int(args.num_train_epochs * num_update_steps_per_epoch)
        else:
            args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

        accelerator = Accelerator(cpu=args.no_cuda, mixed_precision="fp16" if args.fp16 else "no")
        device = accelerator.device

        model = GPT2LMHeadModel.from_pretrained(args.load_model_path,
                                                     cache_dir=cache_dir)
        
        '''
        if args.lora:
            if args.load_ckpt:
                model = PeftModel.from_pretrained(model, args.load_ckpt, is_trainable=True)
            else:
                peft_config = LoraConfig(task_type=TaskType.CAUSAL_LM, r=8, lora_alpha=32, lora_dropout=0.1)
                model = get_peft_model(model, peft_config)
                model.print_trainable_parameters()
        '''
        

        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": args.weight_decay
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0
            }
        ]

        optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
        scheduler = get_scheduler(name=args.lr_scheduler_type,
                                  optimizer=optimizer,
                                  num_warmup_steps=args.max_train_steps * args.warmup_proportion,
                                  num_training_steps=args.max_train_steps)

        if args.do_eval:
            eval_examples = processor.get_dev_examples(os.path.join(args.data_dir, task_name), args.eval_on)
            eval_features = convert_examples_to_features(eval_examples, args.max_seq_length, tokenizer)

            all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
            all_attention_mask = torch.tensor([f.attention_mask for f in eval_features], dtype=torch.long)
            all_labels = torch.tensor([f.labels for f in eval_features], dtype=torch.long)

            eval_data = TensorDataset(all_input_ids, all_attention_mask, all_labels)
            eval_dataloader = DataLoader(eval_data, shuffle=False, batch_size=args.eval_batch_size)

        model, optimizer, scheduler, train_dataloader = accelerator.prepare(model, optimizer, scheduler, train_dataloader)

        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", args.max_train_steps)

        global_step = 0
        best_epoch = 0
        best_result = list()
        progress_bar = tqdm(range(args.max_train_steps))
        for epoch in range(int(args.num_train_epochs)):
            model.train()
            train_loss = 0
            num_train_examples = 0
            train_steps = 0
            for step, batch in enumerate(train_dataloader):
                batch = tuple(t.to(device) for t in batch)
                input_ids, attention_mask, labels = batch
                if args.mft:
                    input_ids = dynamic_mask_token(input_ids, labels, tokenizer, device, noise_probability=args.mask_rate)
                #print("size of input_ids:{}".format(input_ids.size()))
                #print("size of label_ids:{}".format(labels.size()))
                    if step<3:
                        print("input_ids: {}".format(input_ids[0]))
                        print("input_tokens: {}".format(tokenizer.convert_ids_to_tokens(input_ids[0])))

                outputs = model(input_ids=input_ids,
                                attention_mask=attention_mask,
                                labels=labels)
                loss = outputs.loss

                if n_gpu > 1:
                    loss = loss.mean()
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                accelerator.backward(loss)

                train_loss += loss.item()
                num_train_examples += input_ids.size(0)
                train_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                    optimizer.step()
                    optimizer.zero_grad()
                    scheduler.step()
                    global_step += 1
                    progress_bar.update(1)

                '''
                model_to_save = model.module if hasattr(model, "module") else model
                output_model_file = os.path.join(args.output_dir, "checkpoint_ep-{}".format(epoch + 1))
                if (epoch + 1) % 5 == 0:
                    model_to_save.save_pretrained(output_model_file)
                '''

                if args.do_eval  and global_step % args.save_steps == 0:
                    logger.info("***** Running evaluation *****")
                    logger.info("  Num examples = %d", len(eval_examples))
                    logger.info("  Batch size = %d", args.eval_batch_size)
                    def decode(x):
                        return tokenizer.convert_ids_to_tokens(x, skip_special_tokens=True)
                    model.eval()
                    all_inputs, all_predictions, all_labels = [], [], []
                    '''
                    input_ids: cls + src + trg + sep + 0...
                    labels:   ...-100... + trg + sep + -100...
                    '''
                    for i,batch in enumerate(tqdm(eval_dataloader, desc="Evaluation")):
                        batch = tuple(t.to(device) for t in batch)
                        input_ids, attention_mask, labels = batch
                        #if i<3:
                        #    print("inputs: {}".format(input_ids.size()))
                        #    print("labels: {}".format(labels.size()))
                        with torch.no_grad():
                            outputs = model(input_ids=input_ids,
                                            attention_mask=attention_mask,
                                            labels=labels)
                            logits = outputs[1]

                            shift_inputs = input_ids[..., 1:].contiguous()
                            shift_logits = logits[..., :-1, :].contiguous()
                            shift_attention_mask = attention_mask[...,1:].contiguous()
                            shift_labels = labels[..., 1:].contiguous()
                        #(batch,max_seq)
                        prd_ids = shift_logits.argmax(dim=-1)
                        src_ids = shift_inputs.tolist()
                        trg_ids = shift_labels.cpu().numpy().tolist()
                        prd_ids = prd_ids.masked_fill(shift_attention_mask == 0, 0).tolist()
                        if i<3:
                            print("inputs: {}".format(np.array(src_ids).shape))
                            print("predictions: {}".format(np.array(prd_ids).shape))
                            print("labels: {}".format(np.array(trg_ids).shape))
                        for i, (s, t, p) in enumerate(zip(src_ids, trg_ids, prd_ids)):
                            mapped_src = []
                            mapped_trg = []
                            mapped_prd = []
                            flag = False
                            for st, tt, pt in zip(s, t, p):
                                if tt!=-100:
                                    flag=True
                                if not flag:
                                    mapped_src += [st]
                                else:
                                    mapped_trg += [tt if tt!=-100 else 0]
                                    mapped_prd += [pt]
                            all_inputs += [decode(mapped_src)]
                            all_labels += [decode(mapped_trg)]
                            all_predictions += [decode(mapped_prd)]

                    print(all_inputs[0])
                    print(all_labels[0])
                    print(all_predictions[0])

                    output_predict_file = os.path.join(args.output_dir, "predict_results.txt")
                    print("all inputs size: {}".format(len(all_inputs)))
                    print("all predictions size: {}".format(len(all_predictions)))
                    print("all labels size: {}".format(len(all_labels)))

                    train_epoch_loss = train_loss / len(train_dataloader)
                    train_ppl = math.exp(train_epoch_loss)
                    p, r, f1, fpr, tp, fp, fn, wp = Metrics.compute(all_inputs, all_labels, all_predictions)

                    output_tp_file = os.path.join(args.output_dir, "sents.tp")
                    with open(output_tp_file, "w") as writer:
                        for line in tp:
                            writer.write(line + "\n")
                    output_fp_file = os.path.join(args.output_dir, "sents.fp")
                    with open(output_fp_file, "w") as writer:
                        for line in fp:
                            writer.write(line + "\n")
                    output_fn_file = os.path.join(args.output_dir, "sents.fn")
                    with open(output_fn_file, "w") as writer:
                        for line in fn:
                            writer.write(line + "\n")
                    output_wp_file = os.path.join(args.output_dir, "sents.wp")
                    with open(output_wp_file, "w") as writer:
                        for line in wp:
                            writer.write(line + "\n")

                    result = {
                        "global_step": global_step,
                        "train_ppl": train_ppl,
                        "eval_p": p * 100,
                        "eval_r": r * 100,
                        "eval_f1": f1 * 100,
                        "eval_fpr": fpr * 100,
                    }
                    model_to_save = model.module if hasattr(model, "module") else model
                    output_model_file = os.path.join(args.output_dir, "step-%s_f1-%.2f.bin" % (str(global_step), result["eval_f1"]))
                    torch.save(model_to_save.state_dict(), output_model_file)
                    best_result.append((result["eval_f1"], output_model_file))
                    best_result.sort(key=lambda x: x[0], reverse=True)
                    if len(best_result) > 3:
                        _, model_to_remove = best_result.pop()
                        os.remove(model_to_remove)


                    output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                    with open(output_eval_file, "a") as writer:
                        logger.info("***** Eval results *****")
                        writer.write(
                            "Global step = %s | eval precision = %.2f | eval recall = %.2f | eval f1 = %.2f | eval fp rate = %.2f\n"
                            % (str(result["global_step"]),
                            result["eval_p"],
                            result["eval_r"],
                            result["eval_f1"],
                            result["eval_fpr"]))
                        for key in sorted(result.keys()):
                            logger.info("Global step: %s,  %s = %s", str(global_step), key, str(result[key]))

                if global_step >= args.max_train_steps:
                    break
    if args.do_test:
        eval_examples = processor.get_dev_examples(os.path.join(args.data_dir, task_name), args.eval_on)

        logger.info("***** Generation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", 1)

        predict_model = GPT2LMHeadModel.from_pretrained(args.load_model_path,
                                                             cache_dir=cache_dir)
        
        #predict_model = PeftModel.from_pretrained(predict_model, args.load_ckpt)
        #predict_model.print_trainable_parameters()
        predict_model.to(device)
        if args.load_state_dict:
            predict_model.load_state_dict(torch.load(args.load_state_dict))
        predict_model.eval()
        all_inputs, all_labels, all_predictions = [], [], []
        output_predict_file = os.path.join(args.output_dir, "test_results.txt")
        with open(output_predict_file, "w") as writer:
            for i,ex in enumerate(tqdm(eval_examples, desc="Testing")):
                input_ids = tokenizer(ex.source, return_tensors="pt",is_split_into_words=True).input_ids.to(device)
                if i<5:
                    logger.info("input_ids: %s", " ".join([str(x) for x in input_ids]))
                trg = ex.target
                src = ex.source
                with torch.no_grad():
                    ot = predict_model.generate(input_ids=input_ids,
                                                max_new_tokens=64,
                                                eos_token_id=tokenizer.eos_token_id)
                                                
                    pred = tokenizer.convert_ids_to_tokens(ot[0, input_ids.shape[1]:], skip_special_tokens=True)
                    all_inputs+=[src]
                    all_labels+=[trg]
                    all_predictions+=[pred]

                    writer.write(" -> ".join([" ".join(src), " ".join(pred)]) + "\n")
                    

        del predict_model


        p, r, f1, fpr, tp, fp, fn, wp = Metrics.compute(all_inputs, all_labels, all_predictions) ## no need to decode

        output_tp_file = os.path.join(args.output_dir, "sents.tp")
        with open(output_tp_file, "w") as writer:
            for line in tp:
                writer.write(line + "\n")
        output_fp_file = os.path.join(args.output_dir, "sents.fp")
        with open(output_fp_file, "w") as writer:
            for line in fp:
                writer.write(line + "\n")
        output_fn_file = os.path.join(args.output_dir, "sents.fn")
        with open(output_fn_file, "w") as writer:
            for line in fn:
                writer.write(line + "\n")
        output_wp_file = os.path.join(args.output_dir, "sents.wp")
        with open(output_wp_file, "w") as writer:
            for line in wp:
                writer.write(line + "\n")

        result = {
            "eval_p": p * 100,
            "eval_r": r * 100,
            "eval_f1": f1 * 100,
            "eval_fpr": fpr * 100,
        }

        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        with open(output_eval_file, "a") as writer:
            logger.info("***** Eval results *****")
            writer.write(
                "Global step = %s | eval precision = %.2f | eval recall = %.2f | eval f1 = %.2f | eval fp rate = %.2f\n"
                % (str(-1),
                result["eval_p"],
                result["eval_r"],
                result["eval_f1"],
                result["eval_fpr"]))
        for key in sorted(result.keys()):
            logger.info("Global step: %s,  %s = %s", str(-1), key, str(result[key]))



if __name__ == "__main__":
    main()
