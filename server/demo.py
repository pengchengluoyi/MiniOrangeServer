import asyncio
import websockets
import json


async def handler(websocket):
    print("📱 手机端已连接！")
    try:
        # 第一步：处理鉴权
        auth_msg = await websocket.recv()
        print(f"收到鉴权请求: {auth_msg}")

        # 模拟鉴权成功
        await websocket.send(json.dumps({"type": "AUTH_OK"}))
        print("✅ 已下发 AUTH_OK")

        # 第二步：等待 3 秒后，下发截图指令
        await asyncio.sleep(3)
        shot_cmd = {
            "trace_id": "test-shot-1",
            "action_type": "GET_SCREENSHOT",
            "payload": {"quality": 50}
        }
        await websocket.send(json.dumps(shot_cmd))
        print("📸 已下发截图指令")

        # 第三步：处理手机回传的数据
        while True:
            response = await websocket.recv()
            resp_data = json.loads(response)
            if resp_data.get("type") == "SCREENSHOT_RESULT":
                print(f"🎉 收到截图结果！图片 Base64 长度: {len(resp_data['data']['base64_image'])}")

                # 第四步：截图成功后，下发一个点击指令 (测试手势)
                tap_cmd = {
                    "trace_id": "test-tap-1",
                    "action_type": "TAP",
                    "payload": {"x": 500, "y": 800, "duration_ms": 100}
                }
                await websocket.send(json.dumps(tap_cmd))
                print("👇 已下发点击指令 (x:500, y:800)")

            elif resp_data.get("type") == "ACTION_RESULT":
                print(f"✅ 收到手势执行结果: {resp_data}")

    except websockets.ConnectionClosed:
        print("❌ 手机端已断开连接")


async def main():
    print("🚀 测试网关启动，监听 ws://0.0.0.0:8080 ...")
    async with websockets.serve(handler, "0.0.0.0", 8080):
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    asyncio.run(main())