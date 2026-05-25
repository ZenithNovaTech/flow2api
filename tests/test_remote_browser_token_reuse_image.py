import json
import pathlib
import sys
import asyncio
import unittest
from typing import Any, Dict, Optional, Tuple

import httpx

# Allow direct execution via `python tests/...py` from the repo root.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config import config
from src.core.database import Database
from src.services.flow_client import FlowClient
from src.services.proxy_manager import ProxyManager
from src.services.token_manager import TokenManager
from src.services.generation_handler import MODEL_CONFIG


REMOTE_BROWSER_BASE_URL = "http://127.0.0.1:8060"
REMOTE_BROWSER_API_KEY = "fcs_TZ0NA1ymQbw_ApM_ffYFJMKE7XNe9Fr7vBm75BSjVFg"
TEST_MODEL = "gemini-3.1-flash-image-square"
MAX_GENERATION_ATTEMPTS_PER_PROMPT = 3
TEST_PROMPTS = (
    "一个极简风格的白色陶瓷杯，放在木桌上，棚拍光线，干净背景",
    "一只橘猫趴在窗边晒太阳，柔和晨光，写实摄影风格",
    "一辆复古红色自行车停在石板路边，旁边有开满花的咖啡店，电影感构图",
    "雨后的城市街道霓虹倒影，一位撑透明雨伞的人走过，夜景摄影",
    "一盘刚出炉的牛角面包和咖啡，摆在法式早餐桌上，暖色自然光",
    "雪山脚下的木屋，清晨薄雾缭绕，远处有松树林，风景摄影",
    "一只柯基犬在草地上奔跑，舌头吐出，阳光明亮，高速抓拍",
    "现代极简客厅，米色沙发与落地窗，午后阳光洒进室内，室内设计摄影",
    "宇航员站在紫色沙丘上眺望双月天空，科幻概念艺术，细节丰富",
    "蓝色海浪拍打黑色火山岩海岸，天空多云，长曝光摄影风格",
)


class RemoteBrowserTokenReuseImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database()
        self.proxy_manager = ProxyManager(self.db)
        self.flow_client = FlowClient(self.proxy_manager, self.db)
        self.token_manager = TokenManager(self.db, self.flow_client)
        self._original_captcha_method = config.captcha_method

    async def asyncTearDown(self):
        config.set_captcha_method(self._original_captcha_method)
        self.flow_client.clear_request_fingerprint()

    async def test_fetch_remote_browser_token_before_each_image_generation(self):
        await self._ensure_db_ready()
        token, project_id = await self._pick_ready_token_and_project()
        model_config = MODEL_CONFIG[TEST_MODEL]
        generation_results = []

        for index, prompt in enumerate(TEST_PROMPTS, start=1):
            last_error: Optional[Exception] = None

            for attempt in range(1, MAX_GENERATION_ATTEMPTS_PER_PROMPT + 1):
                prefetched = await self._fetch_remote_browser_token(project_id, token.id)
                self.assertTrue(prefetched["token"], f"第 {index} 个 remote_browser token 为空")
                self.assertTrue(prefetched["session_id"], f"第 {index} 个 remote_browser session_id 为空")

                try:
                    self.flow_client._set_request_fingerprint(prefetched["fingerprint"])
                    result, trace = await self._generate_with_supplied_recaptcha_token(
                        at=token.at,
                        project_id=project_id,
                        prompt=prompt,
                        model_name=model_config["model_name"],
                        aspect_ratio=model_config["aspect_ratio"],
                        recaptcha_token=prefetched["token"],
                    )
                    generation_results.append((result, trace, prefetched))
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if not self._is_retryable_recaptcha_error(exc) or attempt >= MAX_GENERATION_ATTEMPTS_PER_PROMPT:
                        raise
                finally:
                    self.flow_client.clear_request_fingerprint()
                    await self._finish_remote_browser_session(prefetched["session_id"])

            if last_error is not None:
                raise last_error

        for index, (result, trace, prefetched) in enumerate(generation_results, start=1):
            self._assert_image_generation_success(result, f"generation-{index}")
            self.assertEqual(trace["recaptcha_token"], prefetched["token"])

    async def _ensure_db_ready(self):
        if not self.db.db_exists():
            self.skipTest("data/flow.db 不存在，无法读取现有 token")

    async def _pick_ready_token_and_project(self):
        tokens = await self.token_manager.get_active_tokens()
        if not tokens:
            self.skipTest("数据库里没有启用中的 token")

        for token in tokens:
            valid_token = await self.token_manager.ensure_valid_token(token)
            if not valid_token or not valid_token.at:
                continue

            project_id = str(valid_token.current_project_id or "").strip()
            if not project_id:
                try:
                    project_id = await self.token_manager.ensure_project_exists(valid_token.id)
                except Exception:
                    continue
            if project_id:
                return valid_token, project_id

        self.skipTest("没有找到可用的 AT/project_id，无法执行真实出图测试")

    async def _fetch_remote_browser_token(self, project_id: str, token_id: Optional[int]) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(base_url=REMOTE_BROWSER_BASE_URL, timeout=45, trust_env=False) as client:
                response = await client.post(
                    "/api/v1/solve",
                    headers={"Authorization": f"Bearer {REMOTE_BROWSER_API_KEY}"},
                    json={
                        "project_id": project_id,
                        "action": "IMAGE_GENERATION",
                        "token_id": token_id,
                    },
                )
        except Exception as exc:
            self.skipTest(f"无法连接远程打码服务 {REMOTE_BROWSER_BASE_URL}: {exc}")

        if response.status_code >= 400:
            self.skipTest(f"远程打码服务返回错误 {response.status_code}: {response.text}")

        try:
            payload = response.json()
        except Exception as exc:
            self.fail(f"远程打码服务返回了非 JSON 响应: {exc}; body={response.text[:500]}")

        remote_token = str(payload.get("token") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        fingerprint = payload.get("fingerprint") if isinstance(payload.get("fingerprint"), dict) else {}
        return {
            "token": remote_token,
            "session_id": session_id,
            "fingerprint": fingerprint,
        }

    async def _finish_remote_browser_session(self, session_id: str):
        if not session_id:
            return
        try:
            async with httpx.AsyncClient(base_url=REMOTE_BROWSER_BASE_URL, timeout=10, trust_env=False) as client:
                await client.post(
                    f"/api/v1/sessions/{session_id}/finish",
                    headers={"Authorization": f"Bearer {REMOTE_BROWSER_API_KEY}"},
                    json={"status": "success"},
                )
        except Exception:
            return

    async def _generate_with_supplied_recaptcha_token(
        self,
        *,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        recaptcha_token: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        url = f"{self.flow_client.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"
        session_id = self.flow_client._generate_session_id()
        client_context = {
            "recaptchaContext": {
                "token": recaptcha_token,
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
            "sessionId": session_id,
            "projectId": project_id,
            "tool": "PINHOLE",
        }
        json_data = {
            "clientContext": client_context,
            "mediaGenerationContext": {"batchId": self.flow_client._generate_scene_id()},
            "useNewMedia": True,
            "requests": [
                {
                    "clientContext": client_context,
                    "seed": 123456,
                    "imageModelName": model_name,
                    "imageAspectRatio": aspect_ratio,
                    "structuredPrompt": {"parts": [{"text": prompt}]},
                    "imageInputs": [],
                }
            ],
        }

        attempt_trace = {"recaptcha_token": recaptcha_token}
        result = await self.flow_client._make_image_generation_request(
            url=url,
            json_data=json_data,
            at=at,
            attempt_trace=attempt_trace,
        )
        return result, attempt_trace

    def _is_retryable_recaptcha_error(self, error: Exception) -> bool:
        error_text = str(error).lower()
        return "recaptcha evaluation failed" in error_text or "public_error_unusual_activity_too_much_traffic" in error_text

    def _assert_image_generation_success(self, result: Dict[str, Any], label: str):
        media_name = self._extract_media_name(result)
        if not media_name:
            pretty = json.dumps(result, ensure_ascii=False)[:1500]
            self.fail(f"{label} image generation 未返回可识别媒体结果: {pretty}")

    def _extract_media_name(self, result: Any) -> Optional[str]:
        if isinstance(result, dict):
            media = result.get("media")
            if isinstance(media, list):
                for item in media:
                    name = self._extract_media_name(item)
                    if name:
                        return name
            name = result.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
            for value in result.values():
                name = self._extract_media_name(value)
                if name:
                    return name
        elif isinstance(result, list):
            for item in result:
                name = self._extract_media_name(item)
                if name:
                    return name
        return None


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    unittest.main()
