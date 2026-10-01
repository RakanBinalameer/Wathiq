#!/usr/bin/env python3
"""Fine-tune Whisper on recorded nursing dictation (Arabic with English medical terms).

STATUS: reference recipe, NOT executed in the hackathon sandbox (needs a GPU + real recordings).
Input  : a CSV manifest  audio_path,text   (16 kHz-compatible audio; text = verified transcript)
         build it from training/out/recording_script.txt read aloud by real nurses (varied dialects).
Install: pip install "transformers>=4.40" datasets accelerate evaluate jiwer librosa soundfile torch
Run    : python training/finetune_whisper.py --manifest data/train/manifest.csv --out models/whisper-nurse
Deploy : ct2-transformers-converter --model models/whisper-nurse --output_dir models/whisper-nurse-ct2 --quantization int8
         export WHISPER_MODEL=models/whisper-nurse-ct2   (app/stt.py picks it up via faster-whisper)
Always hold out nurses (not just sentences) for evaluation: report WER *and* a drug-name accuracy metric.
"""
import argparse
from dataclasses import dataclass

import evaluate
import torch
from datasets import Audio, load_dataset
from transformers import (Seq2SeqTrainer, Seq2SeqTrainingArguments, WhisperForConditionalGeneration,
                          WhisperProcessor)


@dataclass
class Collator:
    processor: WhisperProcessor

    def __call__(self, feats):
        batch = self.processor.feature_extractor.pad([{"input_features": f["input_features"]} for f in feats], return_tensors="pt")
        labels = self.processor.tokenizer.pad([{"input_ids": f["labels"]} for f in feats], return_tensors="pt")
        ids = labels["input_ids"].masked_fill(labels.attention_mask.ne(1), -100)
        if (ids[:, 0] == self.processor.tokenizer.bos_token_id).all():
            ids = ids[:, 1:]
        batch["labels"] = ids
        return batch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--base", default="openai/whisper-small")
    ap.add_argument("--out", default="models/whisper-nurse")
    ap.add_argument("--epochs", type=float, default=5)
    a = ap.parse_args()

    proc = WhisperProcessor.from_pretrained(a.base, language="arabic", task="transcribe")
    model = WhisperForConditionalGeneration.from_pretrained(a.base)
    model.generation_config.language, model.generation_config.task = "arabic", "transcribe"
    model.config.forced_decoder_ids = None

    ds = load_dataset("csv", data_files=a.manifest)["train"].cast_column("audio_path", Audio(sampling_rate=16000))
    ds = ds.train_test_split(test_size=0.1, seed=1)

    def prep(ex):
        au = ex["audio_path"]
        ex["input_features"] = proc.feature_extractor(au["array"], sampling_rate=au["sampling_rate"]).input_features[0]
        ex["labels"] = proc.tokenizer(ex["text"]).input_ids
        return ex

    ds = ds.map(prep, remove_columns=ds["train"].column_names)
    wer = evaluate.load("wer")

    def metrics(pred):
        pred.label_ids[pred.label_ids == -100] = proc.tokenizer.pad_token_id
        return {"wer": 100 * wer.compute(predictions=proc.batch_decode(pred.predictions, skip_special_tokens=True),
                                         references=proc.batch_decode(pred.label_ids, skip_special_tokens=True))}

    args = Seq2SeqTrainingArguments(
        output_dir=a.out, per_device_train_batch_size=16, gradient_accumulation_steps=1, learning_rate=1e-5,
        warmup_steps=200, num_train_epochs=a.epochs, fp16=torch.cuda.is_available(), eval_strategy="epoch",
        save_strategy="epoch", predict_with_generate=True, generation_max_length=225, logging_steps=25,
        load_best_model_at_end=True, metric_for_best_model="wer", greater_is_better=False, report_to="none")
    Seq2SeqTrainer(model=model, args=args, train_dataset=ds["train"], eval_dataset=ds["test"],
                   data_collator=Collator(proc), compute_metrics=metrics, tokenizer=proc.feature_extractor).train()
    model.save_pretrained(a.out)
    proc.save_pretrained(a.out)


if __name__ == "__main__":
    main()
