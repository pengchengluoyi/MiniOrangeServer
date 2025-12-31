# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from script.mPath import get_final_path
from ability.component.template import Template
from ability.component.router import BaseRouter
from ability.engine.vision.mOcr import RapidOCR, cv2, np, analyze, visualize

TAG = "OCR"



@BaseRouter.route('public/ocr')
class FastOCR(Template):
    META = {
        "inputs": [
            {
                "name": "path",
                "type": "file",
                "desc": "文件路径",
                "defaultValue": "screenshot",
                "placeholder": "screenshot"
            },
        ],
        "defaultData": {
            "path": False,
        },
        "outputVars": [
            {"key": "ocr_result", "type": "json", "desc": "图片识别结果"},
            {"key": "ocr_image_path", "type": "str", "desc": "图片识别路径"}
        ]
    }

    def execute(self):
        if cv2 is None or RapidOCR is None or np is None:
            error_msg = f"OCR 依赖库缺失，请安装依赖: pip install opencv-python rapidocr-onnxruntime"
            SLog.e(TAG, error_msg)
            self.result.fail()
            return self.result

        pre_image_path = self.get_param_value("path")
        image_path = get_final_path(pre_image_path)

        try:
            results = analyze(image_path)
            self.memory.set(self.info, "ocr_result", results)

            if results:
                write_path = visualize(image_path, results)
                self.memory.set(self.info, "ocr_image_path", write_path)
            self.result.result_data({"ocr_result": results})
            return self.result.to_dict()

        except Exception as e:
            SLog.e(TAG, f"程序运行出错: {e}")
            import traceback
            traceback.print_exc()
            return self.result.to_dict()


