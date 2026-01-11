# server/core/local_brain.py
import torch
from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
from qwen_vl_utils import process_vision_info
from PIL import Image
import io
import base64
import json
from script.log import SLog


class LocalBrain:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LocalBrain, cls).__new__(cls)
            cls._instance.model = None
            cls._instance.processor = None
        return cls._instance

    def load(self):
        """显式加载模型，避免 import 时卡死"""
        if self.model: return
        SLog.i("LocalBrain", "⏳ Loading Qwen2-VL (Offline VLM)...")
        try:
            # 实际部署路径，不要假设
            model_path = "./models/Qwen2-VL-2B-Instruct-AWQ"
            self.model = Qwen2VLForConditionalGeneration.from_pretrained(
                model_path, torch_dtype=torch.float16, device_map="auto"
            )
            self.processor = AutoProcessor.from_pretrained(model_path)
            SLog.i("LocalBrain", "✅ Model Loaded.")
        except Exception as e:
            SLog.e("LocalBrain", f"❌ Load Failed: {e}")

    def analyze_ui(self, b64_img):
        if not self.model: return None
        try:
            image = Image.open(io.BytesIO(base64.b64decode(b64_img)))
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": "Analyze UI. JSON output: {scene_id, action:{type, target_text, bbox}}"}
                ]
            }]
            text = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = self.processor(text=[text], images=process_vision_info(messages)[0], padding=True,
                                    return_tensors="pt").to("cuda")

            generated_ids = self.model.generate(**inputs, max_new_tokens=128)
            output_text = self.processor.batch_decode(generated_ids, skip_special_tokens=True)[0]

            # 简单提取 JSON
            start = output_text.find('{')
            end = output_text.rfind('}') + 1
            return json.loads(output_text[start:end])
        except Exception as e:
            SLog.e("LocalBrain", f"Inference Error: {e}")
            return None