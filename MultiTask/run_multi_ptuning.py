from __future__ import absolute_import, division, print_function
import argparse
from curses import raw
import logging
import os
import random
import math
import copy
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset
from tqdm import *

import sklearn.metrics as mtc
from scipy.stats import spearmanr
from transformers import AutoTokenizer
from transformers import SchedulerType, get_scheduler
from transformers import BertForMaskedLM

from MultiTask.MultiTaskDatasetPrompt import SighanProcessor, EcspellProcessor, TnewsProcessor, AfqmcProcessor
from MultiTask.MultiTaskDatasetPrompt import csc_convert_examples_to_features, seq_convert_examples_to_features
from MultiTask import MultiTaskDatasetPrompt

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
                    datefmt="%m/%d/%Y %H:%M:%S",
                    level=logging.INFO)
logger = logging.getLogger(__name__)


'''
task_csc = Task(1,'csc','task_classification')
task_tnews = Task(2, 'tnews', 'seq_classification')
task_qmc = Task(3,'afqmc','question-similarity')
'''


class PTuningWrapper(nn.Module):

    def __init__(self, model, tokenizer, verbalizer_tnews, verbalizer_afqmc, prompt_length_sent, prompt_length_csc):
        super().__init__()
        self.config = model.config
        self.tokenizer = tokenizer
        self.prompt_length_csc = prompt_length_csc
        self.prompt_length_sent = prompt_length_sent
        self.verbalizer_tnews = verbalizer_tnews
        self.verbalizer_afqmc = verbalizer_afqmc
        self.tnews_label_words_ids = None if verbalizer_tnews == None else verbalizer_tnews.label_words_ids
        self.afqmc_label_words_ids = None if verbalizer_afqmc == None else verbalizer_afqmc.label_words_ids

        self.csc_num_labels = self.config.vocab_size
        self.tnews_num_labels = None if verbalizer_tnews == None else verbalizer_tnews.num_labels
        self.afqmc_num_labels = None if verbalizer_afqmc == None else verbalizer_afqmc.num_labels

        self.model = model  # mlm
        # the embdedding layer of BERT
        self.model_type = self.config.model_type.split("-")[0]
        self.word_embeddings = getattr(self.model, self.model_type).embeddings.word_embeddings
        
        # pronpt embedding for afqmc
        self.prompt_embeddings_afqmc = nn.Embedding(self.prompt_length_sent, self.config.hidden_size)
        self.prompt_lstm_afqmc = nn.LSTM(input_size=self.config.hidden_size,
                                         hidden_size=self.config.hidden_size,
                                         num_layers=2,
                                         bidirectional=True,
                                         batch_first=True)
        self.prompt_linear_afqmc = nn.Sequential(nn.Linear(2 * self.config.hidden_size, self.config.hidden_size),
                                                 nn.ReLU(),
                                                 nn.Linear(self.config.hidden_size, self.config.hidden_size))

        # prompt embedding for tnews
        self.prompt_embeddings_tnews = nn.Embedding(self.prompt_length_sent, self.config.hidden_size)
        self.prompt_lstm_tnews = nn.LSTM(input_size=self.config.hidden_size,
                                         hidden_size=self.config.hidden_size,
                                         num_layers=2,
                                         bidirectional=True,
                                         batch_first=True)
        self.prompt_linear_tnews = nn.Sequential(nn.Linear(2 * self.config.hidden_size, self.config.hidden_size),
                                                 nn.ReLU(),
                                                 nn.Linear(self.config.hidden_size, self.config.hidden_size))

        # prompt embedding for csc
        self.prompt_embeddings_csc = nn.Embedding(2*self.prompt_length_csc, self.config.hidden_size)
        # LSTM: input:(batch,seq,input_size)-->output[0]:(batch,seq,2*hidden)
        self.prompt_lstm_csc = nn.LSTM(input_size=self.config.hidden_size,
                                       hidden_size=self.config.hidden_size,
                                       num_layers=2,
                                       bidirectional=True,
                                       batch_first=True)
        self.prompt_linear_csc = nn.Sequential(nn.Linear(2 * self.config.hidden_size, self.config.hidden_size),
                                               nn.ReLU(),
                                               nn.Linear(self.config.hidden_size, self.config.hidden_size))

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        prompt_mask=None,
        active_bits=None,
        task_id=None, ## batch
        labels=None,
        inputs_embeds=None,
        output_hidden_states=None,
        return_dict=True
    ):
        # get embdding of all the tasks
        inputs_embeds = self.word_embeddings(input_ids) if inputs_embeds is None else inputs_embeds

        # afqmc
        replace_embeds_afqmc = self.prompt_embeddings_afqmc(torch.LongTensor(list(range(self.prompt_length_sent))).to(inputs_embeds.device))
        replace_embeds_afqmc = replace_embeds_afqmc.unsqueeze(0)
        replace_embeds_afqmc = self.prompt_lstm_afqmc(replace_embeds_afqmc)[0]  # (prompt_length,2*hidden_size)
        replace_embeds_afqmc = self.prompt_linear_afqmc(replace_embeds_afqmc).squeeze()  # (prompt_length,hidden)
        # tnews
        replace_embeds_tnews = self.prompt_embeddings_tnews(torch.LongTensor(list(range(self.prompt_length_sent))).to(inputs_embeds.device))
        replace_embeds_tnews = replace_embeds_tnews.unsqueeze(0)
        replace_embeds_tnews = self.prompt_lstm_tnews(replace_embeds_tnews)[0]  # (prompt_length,2*hidden_size)
        replace_embeds_tnews = self.prompt_linear_tnews(replace_embeds_tnews).squeeze()  # (prompt_length,hidden)
        # csc
        replace_embeds_csc = self.prompt_embeddings_csc(torch.LongTensor(list(range(2*self.prompt_length_csc))).to(input_ids.device))
        replace_embeds_csc = replace_embeds_csc.unsqueeze(0)  # (1,2*prompt_length,hidden_size)
        replace_embeds_csc = self.prompt_lstm_csc(replace_embeds_csc)[0]  # (2*prompt_length,2*hidden_size)
        replace_embeds_csc = self.prompt_linear_csc(replace_embeds_csc).squeeze()  # (2*prompt_length,hidden_size)

        csc_task_filter = (task_id == 1)
        tnews_task_filter = (task_id == 2)
        afqmc_task_filter = (task_id == 3)
        ## prompt_mask (batch,seq)
        # (batch size for csc,seq)
        prompt_mask_csc = prompt_mask[csc_task_filter]
        blocked_indices_csc = (prompt_mask_csc == 1).nonzero().reshape((prompt_mask_csc.shape[0], 2*self.prompt_length_csc, 2))[:, :, 1]  # (batch size for csc,2*prompt_length_csc)
        # (batch size for tnews,seq)
        prompt_mask_tnews = prompt_mask[tnews_task_filter]
        blocked_indices_tnews = (prompt_mask_tnews == 1).nonzero().reshape((prompt_mask_tnews.shape[0], self.prompt_length_sent, 2))[:, :, 1]  # (batch size for tnews,prompt_length_sent)
        # (batch size for afqmc,seq)
        prompt_mask_afqmc = prompt_mask[afqmc_task_filter]
        blocked_indices_afqmc = (prompt_mask_afqmc == 1).nonzero().reshape((prompt_mask_afqmc.shape[0], self.prompt_length_sent, 2))[:, :, 1]  # (batch size for afqmc,prompt_length_sent)

        # replace the prompt positions in input_embeds with prompt embeddings correspondingly
        csc_i, tnews_i, afqmc_i = 0, 0, 0
        for i in range(inputs_embeds.shape[0]):
            if task_id[i] == 1:
                for j in range(blocked_indices_csc.shape[1]):
                    inputs_embeds[i, blocked_indices_csc[csc_i, j],:] = replace_embeds_csc[j, :]
                csc_i += 1
            elif task_id[i] == 2:
                for j in range(blocked_indices_tnews.shape[1]):
                    inputs_embeds[i, blocked_indices_tnews[tnews_i,j], :] = replace_embeds_tnews[j, :]
                tnews_i += 1
            else:
                assert task_id[i] == 3
                for j in range(blocked_indices_afqmc.shape[1]):
                    inputs_embeds[i, blocked_indices_afqmc[afqmc_i,j], :] = replace_embeds_afqmc[j, :]
                afqmc_i += 1

        outputs = self.model(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict
        )
        logits = outputs.logits  # batch,seq,vocab_size
        loss_all = []
        # csc
        if csc_task_filter.any():
            csc_logits = logits[csc_task_filter]
            csc_loss = None
            if labels is not None:
                labels_csc = labels[csc_task_filter]
                input_csc = input_ids[csc_task_filter]
                labels_csc[input_csc == labels_csc] = -100
                loss_fct = nn.CrossEntropyLoss()  # -100 index = padding token
                csc_loss = loss_fct(csc_logits.view(-1, self.csc_num_labels), labels_csc.view(-1))
            logits_output = csc_logits # batch,seq,vocab_size
            loss_all.append(csc_loss)
        # tnews
        if tnews_task_filter.any():
            mask_length = 2
            tnews_logits = logits[tnews_task_filter]  # tnews_batch,seq,vocab
            # tnews_batch,seq
            tnews_active_bits = active_bits[tnews_task_filter]
            tnews_logits = tnews_logits[torch.where(tnews_active_bits != -100)]\
                .view(-1, mask_length, self.tokenizer.vocab_size)  # tnews_batch,mask_length=2,vocab

            # tnews_batch,num_label
            label_words_logits_1 = tnews_logits[:,0, self.tnews_label_words_ids[:, 0]]
            # tnews_batch,num_label
            label_words_logits_2 = tnews_logits[:,1, self.tnews_label_words_ids[:, 1]]
            label_words_logits = label_words_logits_1 * label_words_logits_2
            assert label_words_logits.shape[-1] == self.tnews_num_labels
            tnews_loss = None
            if labels is not None:
                ## batch,seq-->batch
                labels_tnews = labels[tnews_task_filter][:,0]
                loss_fct = nn.CrossEntropyLoss()
                tnews_loss = loss_fct(label_words_logits.view(-1, self.tnews_num_labels), labels_tnews.view(-1))
            logits_output = label_words_logits # afqmc_batch,num_label
            loss_all.append(tnews_loss)
        # afqmc
        if afqmc_task_filter.any():
            afqmc_logits = logits[afqmc_task_filter]  # afqmc_batch,seq,vocab
            # afqmc_batch,seq
            afqmc_active_bits = active_bits[afqmc_task_filter]
            afqmc_logits = afqmc_logits[torch.where(afqmc_active_bits != -100)]  # afqmc_batch,vocab

            label_words_logits = afqmc_logits[:, self.afqmc_label_words_ids] # afqmc_batch,num_label,num_label_mapping
            label_words_logits = torch.sum(label_words_logits, dim=-1)  # afqmc_batch,num_label
            afqmc_loss = None
            if labels is not None:
                labels_afqmc = labels[afqmc_task_filter][:,0]
                loss_fct = nn.CrossEntropyLoss()
                afqmc_loss = loss_fct(label_words_logits.view(-1, self.afqmc_num_labels), labels_afqmc.view(-1))
            logits_output = label_words_logits # afqmc_batch,num_label
            loss_all.append(afqmc_loss)
        loss = torch.stack(loss_all).mean() ## cancatenate all the loss
        if output_hidden_states:
            return loss, logits_output, outputs.hidden_states
        return loss, logits_output


class Metrics:
    # metrics for sequence classification
    @staticmethod
    def acc(predictions, labels):
        return mtc.accuracy_score(labels, predictions)

    @staticmethod
    def mcc(predictions, labels):
        return mtc.matthews_corrcoef(labels, predictions)

    @staticmethod
    def spc(predictions, labels):
        return spearmanr(labels, predictions)[0]

    @staticmethod
    def f1(predictions, labels, average="micro"):
        return mtc.f1_score(labels, predictions, average=average)
    # metrics for csc

    @staticmethod
    def csc_compute(src_sents, trg_sents, prd_sents):
        def difference(src, trg):
            ret = copy.deepcopy(src)
            for i, (src_char, trg_char) in enumerate(zip(src, trg)):
                if src_char != trg_char:
                    ret[i] = "(" + src_char + "->" + trg_char + ")"

            return "".join(ret)

        pos_sents, neg_sents, tp_sents, fp_sents, fn_sents, prd_pos_sents, prd_neg_sents, wp_sents = [], [], [], [], [], [], [], []
        for s, t, p in zip(src_sents, trg_sents, prd_sents):
            # For positive examples
            if s != t:
                pos_sents.append(difference(s, t))
                if p == t:
                    tp_sents.append(difference(s, t))
                if p == s:
                    fn_sents.append(difference(s, t))
                if (p != t and p != s):
                    wp_sents.append(difference(s, t))
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


def main():
    parser = argparse.ArgumentParser()

    # Data config.
    parser.add_argument("--data_dir", type=str, default="data/",
                        help="Directory to contain the input data for all tasks.")
    # mulitple tasks splited by " "
    parser.add_argument("--task_name", type=str, default="SIGHAN tnews afqmc",
                        help="Name of the training task.")
    parser.add_argument("--load_model_path", type=str, default="bert-base-chinese",
                        help="Pre-trained model path to load if needed.")
    parser.add_argument("--cache_dir", type=str, default="../cache/",
                        help="Directory to store the pre-trained language models downloaded from s3.")
    parser.add_argument("--output_dir", type=str, default="model/",
                        help="Directory to output predictions and checkpoints.")
    parser.add_argument("--load_state_dict", type=str, default="",
                        help="Trained model weights to load for evaluation.")

    # Training config.
    parser.add_argument("--do_train", action="store_true",
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action="store_true",
                        help="Whether to evaluate on the dev set.")
    parser.add_argument("--do_test", action="store_true",
                        help="Whether to evaluate on the test set.")
    # multiple datasets splited by " "
    parser.add_argument("--train_on", type=str, default="hybrid base base",
                        help="Choose a training set.")
    # eval and test on only one task
    parser.add_argument("--eval_on", type=str, default="15",
                        help="Choose a dev set.")
    parser.add_argument("--test_on", type=str, default="15",
                        help="Choose a test set.")
    parser.add_argument("--use_slow_tokenizer", action="store_true",
                        help="A slow tokenizer will be used if passed.")
    parser.add_argument("--do_lower_case", action="store_true",
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--max_seq_length", type=int, default=128,
                        help="Maximum total input sequence length after word-piece tokenization\
                            for the two sentence classification tasks.")
    parser.add_argument("--train_batch_size", type=int, default=128,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", type=int, default=512,
                        help="Total batch size for evaluation.")
    parser.add_argument("--learning_rate", type=float, default=5e-5,
                        help="Initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", type=float, default=3.0,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_train_steps", type=int, default=None,
                        help="Total number of training steps to perform. If provided, overrides training epochs.")
    parser.add_argument("--lr_scheduler_type", type=SchedulerType, default="linear",
                        help="Scheduler type for learning rate warmup.")
    parser.add_argument("--warmup_proportion", type=float, default=0.1,
                        help="Proportion of training to perform learning rate warmup for.")
    parser.add_argument("--weight_decay", type=float, default=0.,
                        help="L2 weight decay for training.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward pass.")
    parser.add_argument("--no_cuda", action="store_true",
                        help="Whether not to use CUDA when available.")
    parser.add_argument("--fp16", action="store_true",
                        help="Whether to use mixed precision.")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for initialization.")
    parser.add_argument("--save_steps", type=int, default=100,
                        help="How many steps to save the checkpoint once.")
    parser.add_argument("--mft", action="store_true",
                        help="Training with masked-fine-tuning (not published yet).")
    parser.add_argument("--mask_mode", type=str, default="noerror", help="noerror,error or all")

    parser.add_argument("--csc_prompt_length", type=int,
                        default=3, help="the length of the continuous prompt")
    parser.add_argument("--sent_prompt_length", type=int,
                        default=3, help="the length of the continuous prompt")
    parser.add_argument("--anchor",type=str,default=None,help="the anchor tokens we add to the prompt.")
    parser.add_argument("--freeze_lm", action="store_true",
                        help="Whether to keep LM parameters frozen.")

    args = parser.parse_args()

    processors_all = {
        "sighan": SighanProcessor,
        "ecspell": EcspellProcessor,
        "sghspell": SighanProcessor,  # the data format in sghspell is the same as sighan
        "tnews": TnewsProcessor,
        "afqmc": AfqmcProcessor,
    }

    task_class = {"csc": ["sighan", "ecspell", "sghspell"],
                  "seq": ["tnews", "afqmc"]}

    device = torch.device("cuda" if torch.cuda.is_available()
                          and not args.no_cuda else "cpu")
    n_gpu = torch.cuda.device_count()
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, "Unsupported", args.fp16))

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

    task_names = args.task_name.lower().split()
    train_on_list = args.train_on.lower().split()
    for task_name in task_names:
        if task_name not in processors_all:
            raise ValueError("Task not found: %s" % task_name)
    # processors is a map containing all the processors we will use
    processors = {}
    train_on_dataset = {}
    for task_name in task_names:
        processors[task_name] = processors_all[task_name]()
    for train_on, task_name in zip(train_on_list, task_names):
        train_on_dataset[task_name] = train_on

    cache_dir = args.cache_dir
    tokenizer = AutoTokenizer.from_pretrained(args.load_model_path,
                                              do_lower_case=args.do_lower_case,
                                              cache_dir=cache_dir,
                                              use_fast=not args.use_slow_tokenizer,
                                              add_prefix_space=True)
    ## initialize verbalizers we are going to use
    verbalizers = {}
    for task_name in task_names:
        if task_name in  task_class["seq"]:
            verbalizers[task_name] = MultiTaskDatasetPrompt.Verbalizer(processors[task_name].get_label_template(), tokenizer)


    max_seq_length_csc = args.max_seq_length+args.sent_prompt_length+2
    max_seq_length_sent = args.max_seq_length

    anchor=None
    if args.anchor is not None:
        anchor=[tokenizer.sep_token]+[t for t in args.anchor]

    if args.do_train:
        train_examples = [] ## train examples of all tasks
        train_features = [] ## train features of all tasks
        for task_name, processor in processors.items():
            ## train examples of this task
            train_examples_ = processor.get_train_examples(os.path.join(
                args.data_dir, task_name), train_on_dataset[task_name])
            train_examples += train_examples_
            if task_name in task_class["csc"]:
                train_features += csc_convert_examples_to_features(train_examples_, max_seq_length_csc, tokenizer,
                                                                   args.csc_prompt_length, args.mft, args.mask_mode, anchor)
            else:
                assert(task_name in task_class["seq"])
                label_list = processor.get_labels()
                print(label_list)
                mask_length = 0
                if task_name == "afqmc":
                    mask_length = 1
                else:
                    assert task_name == "tnews"
                    mask_length = 2
                assert mask_length != 0
                train_features += seq_convert_examples_to_features(train_examples_, label_list, args.sent_prompt_length,
                                                                   mask_length, max_seq_length_sent, tokenizer)
        all_input_ids = torch.tensor([f.input_ids for f in train_features], dtype=torch.long)
        # attention_mask
        all_input_mask = torch.tensor([f.input_mask for f in train_features], dtype=torch.long)
        # token_type_ids
        all_input_segment = torch.tensor([f.token_type_ids for f in train_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_ids for f in train_features], dtype=torch.long)  # (batch,seq)
        all_task_ids = torch.tensor([f.task_id for f in train_features], dtype=torch.long)
        all_prompt_mask = torch.tensor([f.prompt_mask for f in train_features], dtype=torch.long)
        all_active_bits = torch.tensor([f.active_bits for f in train_features], dtype=torch.long)

        train_data = TensorDataset(all_input_ids, all_input_mask, all_input_segment,
                                   all_label_ids, all_task_ids, all_prompt_mask, all_active_bits)
        # we have to disrupt the order the features from different tasks
        train_sampler = RandomSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
        # len(train_dataloader)=len(examples)/batch_size
        if args.max_train_steps is None:
            args.max_train_steps = int(
                args.num_train_epochs * num_update_steps_per_epoch)
        else:
            args.num_train_epochs = math.ceil(
                args.max_train_steps / num_update_steps_per_epoch)

        # we use mlm to do correction and classification
        model = BertForMaskedLM.from_pretrained(args.load_model_path,
                                                return_dict=True,
                                                cache_dir=cache_dir)
        verbalizer_tnews, verbalizer_afqmc = None, None
        if "tnews" in verbalizers.keys():
            verbalizer_tnews = verbalizers["tnews"]
        if "afqmc" in verbalizers.keys():
            verbalizer_afqmc = verbalizers["afqmc"]
        model = PTuningWrapper(model, tokenizer, verbalizer_tnews, verbalizer_afqmc,
                               args.sent_prompt_length, args.csc_prompt_length)  # apply p-tuning(prompt) to the model
        model.to(device)
        if args.load_state_dict:
            model.load_state_dict(torch.load(args.load_state_dict))
        if n_gpu > 1:
            # It is recommended to use DistributedDataParallel
            model = torch.nn.DataParallel(model)

        no_decay = ["bias", "LayerNorm.bias", "LayerNorm.weight"]
        # apply weight decay
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
        # set the Adam optimizer
        optimizer = torch.optim.AdamW(
            optimizer_grouped_parameters, lr=args.learning_rate)
        scheduler = get_scheduler(name=args.lr_scheduler_type,
                                  optimizer=optimizer,
                                  num_warmup_steps=args.max_train_steps * args.warmup_proportion,
                                  num_training_steps=args.max_train_steps)
        #######################################################################
        if args.freeze_lm:  # freeze the parameters in the lm except prompt parameters
            prompt_params = ["prompt_"]
            for n, p in model.named_parameters():
                if not any(nd in n for nd in prompt_params):  # why not nd==n
                    p.requires_grad = False
                    logger.info("Freeze `{}`".format(n))

        scaler = None
        if args.fp16:  # use half precision to reduce the memory usage of neural networks
            from torch.cuda.amp import autocast, GradScaler

            scaler = GradScaler()

        if args.do_eval:
            task_name = task_names[0]  # we choose the first task to evaluate
            processor = processors[task_name]
            eval_examples = processor.get_test_examples(os.path.join(args.data_dir, task_name), args.eval_on)

            if task_name in task_class["csc"]:
                eval_features = csc_convert_examples_to_features(eval_examples, max_seq_length_csc, tokenizer,
                                                                   args.csc_prompt_length, anchor)
            else:
                assert(task_name in task_class["seq"])
                label_list = processor.get_labels()
                print(label_list)
                mask_length = 0
                if task_name == "afqmc":
                    mask_length = 1
                else:
                    assert task_name == "tnews"
                    mask_length = 2
                assert mask_length != 0
                eval_features = seq_convert_examples_to_features(eval_examples, label_list, args.sent_prompt_length,
                                                                   mask_length, max_seq_length_sent, tokenizer)
            all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
            # attention_mask
            all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
            # token_type_ids
            all_input_segment = torch.tensor([f.token_type_ids for f in eval_features], dtype=torch.long)
            all_label_ids = torch.tensor([f.label_ids for f in eval_features], dtype=torch.long)  # (batch,seq)
            all_task_ids = torch.tensor([f.task_id for f in eval_features], dtype=torch.long)
            all_prompt_mask = torch.tensor([f.prompt_mask for f in eval_features], dtype=torch.long)
            all_active_bits = torch.tensor([f.active_bits for f in eval_features], dtype=torch.long)

            eval_data = TensorDataset(all_input_ids, all_input_mask, all_input_segment,
                                    all_label_ids, all_task_ids, all_prompt_mask, all_active_bits)
            # we have to disrupt the order the features from different tasks
            eval_sampler = RandomSampler(eval_data)
            eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

    if args.do_train:
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", args.max_train_steps)

        global_step = 0
        best_result = list()
        wrap = False
        progress_bar = tqdm(range(args.max_train_steps))
        for _ in range(int(args.num_train_epochs)):
            train_loss = 0
            num_train_examples = 0
            if wrap: break
            for step, batch in enumerate(train_dataloader):
                model.train()
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, input_segment,label_ids, task_ids, prompt_mask, active_bits = batch

                if args.fp16:
                    with autocast():
                        outputs = model(input_ids=input_ids,
                                        attention_mask=input_mask,
                                        token_type_ids=input_segment,
                                        prompt_mask=prompt_mask,
                                        active_bits=active_bits,
                                        task_id=task_ids,
                                        labels=label_ids,)
                else:
                    outputs = model(input_ids=input_ids,
                                    attention_mask=input_mask,
                                    token_type_ids=input_segment,
                                    prompt_mask=prompt_mask,
                                    active_bits=active_bits,
                                    task_id=task_ids,
                                    labels=label_ids,)
                loss = outputs[0]

                loss = outputs[0]

                if n_gpu > 1:
                    loss = loss.mean()
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                if args.fp16:
                    scaler.scale(loss).backward()
                else:
                    loss.backward()

                train_loss += loss.item()
                num_train_examples += input_ids.size(0)
                if (step + 1) % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                    if args.fp16:
                        scaler.unscale_(optimizer)
                        scaler.step(optimizer)
                        scaler.update()
                    else:
                        optimizer.step()
                    optimizer.zero_grad()
                    scheduler.step()
                    global_step += 1
                    progress_bar.update(1)

                if args.do_eval and global_step % args.save_steps == 0:
                    logger.info("***** Running evaluation *****")
                    logger.info("  Num examples = %d", len(eval_examples))
                    logger.info("  Batch size = %d", args.eval_batch_size)

                    def decode(input_ids):
                        return tokenizer.convert_ids_to_tokens(input_ids, skip_special_tokens=True)
                    
                    model.eval()
                    eval_loss = 0
                    eval_steps = 0
                    all_inputs, all_labels, all_predictions = [], [], []
                    for batch in tqdm(eval_dataloader, desc="Evaluation"):
                        batch = tuple(t.to(device) for t in batch)
                        input_ids, input_mask, input_segment,label_ids, task_ids, prompt_mask, active_bits = batch
                        with torch.no_grad():
                            outputs = model(input_ids=input_ids,
                                            attention_mask=input_mask,
                                            token_type_ids=input_segment,
                                            prompt_mask=prompt_mask,
                                            active_bits=active_bits,
                                            task_id=task_ids,
                                            labels=label_ids,)
                            tmp_eval_loss = outputs[0]
                            logits = outputs[1] ##(batch_size,seq_length,vocab_size) or (batch_size,label_list_size)

                        src_ids = input_ids.cpu().tolist() ##(batch_size,seq_length)
                        trg_ids = label_ids.cpu().numpy() ##(batch_size,seq_length)
                        eval_loss += tmp_eval_loss.mean().item()
                        _, prd_ids = torch.max(logits, -1) ##(batch_size,seq_length) or (batch_size)

                        if task_name in task_class["csc"]:
                            prd_ids = prd_ids.masked_fill(input_mask == 0, 0).tolist()##set the padding part to 0
                            for s, t, p in zip(src_ids, trg_ids, prd_ids):
                                mapped_src = []
                                mapped_trg = []
                                mapped_prd = []
                                flag = False## if we pass to the target part
                                ##src: [CLS]+[CLS]...+src+[SEP]...+[mask]
                                ##trg: [CLS]+[CLS]...+src+[SEP]...+trg
                                for st, tt, pt in zip(s, t, p):
                                    if st == tokenizer.sep_token_id:
                                        flag = True
                                    if not flag:
                                        mapped_src += [st]
                                    else:
                                        mapped_trg += [tt]
                                        if st == tokenizer.mask_token_id:##we only predict the masked tokens
                                            mapped_prd += [pt]
                                        else:
                                            mapped_prd += [st]
                                if anchor is not None:
                                    ##src: [CLS]+[CLS]...+src+anchor+[SEP]...+[mask]
                                    ##trg: [CLS]+[CLS]...+src+anchor+[SEP]...+trg
                                    ## remove the anchor tokens from the src
                                    anchor_length = len(anchor)
                                    del mapped_trg[:anchor_length]
                                    del mapped_prd[:anchor_length]
                                ## we skip special tokens including '[UNK]','[SEP]'
                                all_inputs += [decode(mapped_src)]
                                all_labels += [decode(mapped_trg)]
                                all_predictions += [decode(mapped_prd)]
                        else:
                            assert(task_name in task_class["seq"])
                            all_predictions.extend(prd_ids.detach().cpu().numpy().squeeze().tolist())
                            all_labels.extend(trg_ids[:,0].squeeze().tolist())
                        eval_steps += 1
                    
                    loss = train_loss / global_step
                    eval_loss = eval_loss / eval_steps
                    if task_name in task_class["csc"]:
                        p, r, f1, fpr, tp, fp, fn, wp = Metrics.csc_compute(all_inputs, all_labels, all_predictions)
                    else:
                        assert(task_name in task_class["seq"])
                        f1 = Metrics.f1(all_predictions, all_labels)
                        acc = Metrics.acc(all_predictions,all_labels)

                    if task_name in task_class["csc"]:
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
                            "loss": loss,
                            "eval_loss": eval_loss,
                            "eval_p": p * 100,
                            "eval_r": r * 100,
                            "eval_f1": f1 * 100,
                            "eval_fpr": fpr * 100,
                        }
                    else:
                        result = {
                            "global_step": global_step,
                            "loss": loss,
                            "eval_loss": eval_loss,
                            "eval_acc": acc*100,
                            "eval_f1": f1 * 100,
                        }
                    model_to_save = model.module if hasattr(model, "module") else model
                    output_model_file = os.path.join(args.output_dir, "step-%s_f1-%.2f.bin" % (str(global_step), result["eval_f1"]))
                    torch.save(model_to_save.state_dict(), output_model_file)
                    best_result.append((result["eval_f1"], output_model_file))
                    ## sort by f1 and remove model whose f1 is the fourth biggest 
                    best_result.sort(key=lambda x: x[0], reverse=True)
                    if len(best_result) > 3:
                        _, model_to_remove = best_result.pop()
                        os.remove(model_to_remove)

                    output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                    if task_name in task_class['csc']:
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
                    else:
                        with open(output_eval_file, "a") as writer:
                            logger.info("***** Eval results *****")
                            writer.write(
                                "Global step = %s |  eval f1 = %.2f |  eval acc = %.2f \n"
                                % (str(result["global_step"]),
                                result["eval_f1"],
                                result["eval_acc"]))
                            for key in sorted(result.keys()):
                                logger.info("Global step: %s,  %s = %s", str(global_step), key, str(result[key]))

                if global_step >= args.max_train_steps:
                    wrap = True
                    break

    if args.do_test:
        task_name = task_names[0]  # we choose the first task to evaluate
        processor = processors[task_name]
        eval_examples = processor.get_test_examples(os.path.join(args.data_dir, task_name), args.test_on)

        if task_name in task_class["csc"]:
            eval_features = csc_convert_examples_to_features(eval_examples, max_seq_length_csc, tokenizer,
                                                                args.csc_prompt_length, anchor)
        else:
            assert(task_name in task_class["seq"])
            label_list = processor.get_labels()
            print(label_list)
            mask_length = 0
            if task_name == "afqmc":
                mask_length = 1
            else:
                assert task_name == "tnews"
                mask_length = 2
            assert mask_length != 0
            eval_features = seq_convert_examples_to_features(eval_examples, label_list, args.sent_prompt_length,
                                                                mask_length, max_seq_length_sent, tokenizer)
        all_input_ids = torch.tensor([f.input_ids for f in eval_features], dtype=torch.long)
        # attention_mask
        all_input_mask = torch.tensor([f.input_mask for f in eval_features], dtype=torch.long)
        # token_type_ids
        all_input_segment = torch.tensor([f.token_type_ids for f in eval_features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_ids for f in eval_features], dtype=torch.long)  # (batch,seq)
        all_task_ids = torch.tensor([f.task_id for f in eval_features], dtype=torch.long)
        all_prompt_mask = torch.tensor([f.prompt_mask for f in eval_features], dtype=torch.long)
        all_active_bits = torch.tensor([f.active_bits for f in eval_features], dtype=torch.long)

        eval_data = TensorDataset(all_input_ids, all_input_mask, all_input_segment,
                                all_label_ids, all_task_ids, all_prompt_mask, all_active_bits)
        # we have to disrupt the order the features from different tasks
        eval_sampler = RandomSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

        # we use mlm to do correction and classification
        model = BertForMaskedLM.from_pretrained(args.load_model_path,
                                                return_dict=True,
                                                cache_dir=cache_dir)
        verbalizer_tnews, verbalizer_afqmc = None, None
        if "tnews" in verbalizers.keys():
            verbalizer_tnews = verbalizers["tnews"]
        if "afqmc" in verbalizers.keys():
            verbalizer_afqmc = verbalizers["afqmc"]
        model = PTuningWrapper(model, tokenizer, verbalizer_tnews, verbalizer_afqmc,
                               args.sent_prompt_length, args.csc_prompt_length)  # apply p-tuning(prompt) to the model
        model.to(device)
        ## load the checkpoints to do test
        if args.load_state_dict:
            model.load_state_dict(torch.load(args.load_state_dict))
        if n_gpu > 1:
            # It is recommended to use DistributedDataParallel
            model = torch.nn.DataParallel(model)

        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)

        def decode(input_ids):
            return tokenizer.convert_ids_to_tokens(input_ids, skip_special_tokens=True)
        
        model.eval()
        eval_loss = 0
        eval_steps = 0
        all_inputs, all_labels, all_predictions = [], [], []
        for batch in tqdm(eval_dataloader, desc="Evaluation"):
            batch = tuple(t.to(device) for t in batch)
            input_ids, input_mask, input_segment,label_ids, task_ids, prompt_mask, active_bits = batch
            with torch.no_grad():
                outputs = model(input_ids=input_ids,
                                attention_mask=input_mask,
                                token_type_ids=input_segment,
                                prompt_mask=prompt_mask,
                                active_bits=active_bits,
                                task_id=task_ids,
                                labels=label_ids,)
                tmp_eval_loss = outputs[0]
                logits = outputs[1] ##(batch_size,seq_length,vocab_size) or (batch_size,label_list_size)

            src_ids = input_ids.cpu().tolist() ##(batch_size,seq_length)
            trg_ids = label_ids.cpu().numpy() ##(batch_size,seq_length)
            eval_loss += tmp_eval_loss.mean().item()
            _, prd_ids = torch.max(logits, -1) ##(batch_size,seq_length) or (batch_size)

            if task_name in task_class["csc"]:
                prd_ids = prd_ids.masked_fill(input_mask == 0, 0).tolist()##set the padding part to 0
                for s, t, p in zip(src_ids, trg_ids, prd_ids):
                    mapped_src = []
                    mapped_trg = []
                    mapped_prd = []
                    flag = False## if we pass to the target part
                    ##src: [CLS]+[CLS]...+src+[SEP]...+[mask]
                    ##trg: [CLS]+[CLS]...+src+[SEP]...+trg
                    for st, tt, pt in zip(s, t, p):
                        if st == tokenizer.sep_token_id:
                            flag = True
                        if not flag:
                            mapped_src += [st]
                        else:
                            mapped_trg += [tt]
                            if st == tokenizer.mask_token_id:##we only predict the masked tokens
                                mapped_prd += [pt]
                            else:
                                mapped_prd += [st]
                    if anchor is not None:
                        ##src: [CLS]+[CLS]...+src+anchor+[SEP]...+[mask]
                        ##trg: [CLS]+[CLS]...+src+anchor+[SEP]...+trg
                        ## remove the anchor tokens from the src
                        anchor_length = len(anchor)
                        del mapped_trg[:anchor_length]
                        del mapped_prd[:anchor_length]
                    ## we skip special tokens including '[UNK]','[SEP]'
                    all_inputs += [decode(mapped_src)]
                    all_labels += [decode(mapped_trg)]
                    all_predictions += [decode(mapped_prd)]
            else:
                assert(task_name in task_class["seq"])
                all_predictions.extend(prd_ids.detach().cpu().numpy().squeeze().tolist())
                all_labels.extend(trg_ids[:,0].squeeze().tolist())
            eval_steps += 1
        
        eval_loss = eval_loss / eval_steps
        if task_name in task_class["csc"]:
            p, r, f1, fpr, tp, fp, fn, wp = Metrics.csc_compute(all_inputs, all_labels, all_predictions)
        else:
            assert(task_name in task_class["seq"])
            f1 = Metrics.f1(all_predictions, all_labels)
            acc = Metrics.acc(all_predictions,all_labels)

        if task_name in task_class["csc"]:
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
                "eval_step": eval_steps,
                "eval_loss": eval_loss,
                "eval_p": p * 100,
                "eval_r": r * 100,
                "eval_f1": f1 * 100,
                "eval_fpr": fpr * 100,
            }
        else:
            result = {
                "eval_step": eval_steps,
                "eval_loss": eval_loss,
                "eval_acc": acc*100,
                "eval_f1": f1 * 100,
            }
        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        if task_name in task_class['csc']:
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
        else:
            with open(output_eval_file, "a") as writer:
                logger.info("***** Eval results *****")
                writer.write(
                    "Global step = %s |  eval f1 = %.2f |  eval acc = %.2f \n"
                    % (str(-1),
                    result["eval_f1"],
                    result["eval_acc"]))
                for key in sorted(result.keys()):
                    logger.info("Global step: %s,  %s = %s", str(-1), key, str(result[key]))


if __name__ == "__main__":
    main()