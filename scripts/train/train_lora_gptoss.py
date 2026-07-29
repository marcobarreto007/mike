"""
LoRA fine-tuning script for gpt-oss-20b on Vast.ai
Optimized for RTX 5090 32GB | Julho 2026
Usage: python train_lora_gptoss.py --dataset meus_dados.jsonl
"""
import argparse, json, os, torch
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────
MODEL_ID = "openai/gpt-oss-20b"  # ou abliterated: "kabachuha/gpt-oss-20b-SOMbliterated"
OUTPUT_DIR = "./lora_output"
LORA_R = 16          # rank (16-64, maior = mais capacidade mas overfitting)
LORA_ALPHA = 32      # alpha = 2× rank é padrão seguro
LORA_DROPOUT = 0.05
TARGET_MODULES = [    # módulos a treinar (só estes recebem LoRA)
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]
LEARNING_RATE = 2e-4
NUM_EPOCHS = 3
BATCH_SIZE = 4       # ajusta conforme VRAM (5090 32GB aguenta 8+)
GRAD_ACCUM = 4       # batch efetivo = BATCH_SIZE × GRAD_ACCUM
MAX_SEQ_LENGTH = 2048
WARMUP_RATIO = 0.1
SAVE_STEPS = 200
LOGGING_STEPS = 10


def load_dataset(path: str):
    """Carrega dataset em formato JSONL (um JSON por linha)."""
    samples = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(f"[DATA] Loaded {len(samples)} samples from {path}")
    return samples


def format_sample(sample: dict) -> str:
    """
    Formata uma amostra no chat template do gpt-oss-20b (harmony).
    Exemplo de entrada:
      {"messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
    Exemplo de entrada simples:
      {"instruction": "...", "output": "..."}
    """
    if "messages" in sample:
        # Formato conversation-style (preferido)
        return sample  # retorna como está, o tokenizer aplica o template

    # Formato instruction-output (converte para messages)
    instruction = sample.get("instruction", sample.get("input", ""))
    output = sample.get("output", sample.get("response", ""))

    return {
        "messages": [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": output},
        ]
    }


def train(args):
    from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments, Trainer
    from peft import LoraConfig, get_peft_model, TaskType
    from datasets import Dataset
    import bitsandbytes as bnb

    print(f"[TRAIN] Starting LoRA training at {datetime.now()}")
    print(f"[TRAIN] Base model: {MODEL_ID}")
    print(f"[TRAIN] Output dir: {OUTPUT_DIR}")

    # ── Load model with 4-bit quantization ──
    print("[MODEL] Loading base model in 4-bit...")
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_ID,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        trust_remote_code=True,
    )

    # ── Load tokenizer ──
    print("[MODEL] Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # ── Configure LoRA ──
    print(f"[LORA] Configuring LoRA r={LORA_R} alpha={LORA_ALPHA}...")
    lora_config = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=TARGET_MODULES,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── Load and format dataset ──
    raw = load_dataset(args.dataset)
    formatted = [format_sample(s) for s in raw]

    def tokenize(sample):
        msgs = sample["messages"]
        text = tokenizer.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=False
        )
        return tokenizer(
            text,
            truncation=True,
            max_length=MAX_SEQ_LENGTH,
            padding=False,
        )

    dataset = Dataset.from_list(formatted)
    dataset = dataset.map(tokenize, remove_columns=dataset.column_names)

    # ── Training args ──
    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        per_device_train_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACCUM,
        num_train_epochs=NUM_EPOCHS,
        learning_rate=LEARNING_RATE,
        warmup_ratio=WARMUP_RATIO,
        logging_steps=LOGGING_STEPS,
        save_steps=SAVE_STEPS,
        save_total_limit=3,
        bf16=True,
        gradient_checkpointing=True,
        optim="adamw_8bit",
        report_to="none",
        remove_unused_columns=False,
    )

    # ── Train ──
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
    )

    print(f"[TRAIN] Starting training for {NUM_EPOCHS} epochs...")
    trainer.train()

    # ── Save ──
    final_path = Path(OUTPUT_DIR) / "final_lora"
    model.save_pretrained(str(final_path))
    tokenizer.save_pretrained(str(final_path))
    print(f"[TRAIN] LoRA weights saved to {final_path}")
    print(f"[TRAIN] Copy this folder to MIKE: llm_cache/lora/{final_path.name}/")
    print(f"[TRAIN] DONE at {datetime.now()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LoRA train gpt-oss-20b")
    parser.add_argument("--dataset", required=True, help="JSONL dataset path")
    parser.add_argument("--model", default=MODEL_ID, help="Base model ID")
    parser.add_argument("--output", default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--epochs", type=int, default=NUM_EPOCHS)
    parser.add_argument("--lora_r", type=int, default=LORA_R)
    args = parser.parse_args()

    MODEL_ID = args.model
    OUTPUT_DIR = args.output
    NUM_EPOCHS = args.epochs
    LORA_R = args.lora_r

    train(args)
