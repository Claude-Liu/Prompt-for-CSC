CUDA_VISIBLE_DEVICES=0 python run_relm_multi.py \
    --do_train \
    --do_eval \
    --mft \
    --mask_mode "noerror" \
    --mask_rate 0.3 \
    --task_name "ecspell tnews afqmc"  \
    --train_on "law base base"  \
    --eval_on 'law' \
    --csc_prompt_length 10 \
    --sent_prompt_length 3 \
    --save_steps 1000 \
    --learning_rate 5e-5 \
    --num_train_epochs 20.0 \
    --train_batch_size 128 \
    --eval_batch_size 64 \
    --fp16 \
    --output_dir "model/model_multi" 