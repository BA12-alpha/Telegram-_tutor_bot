import os, json, requests, logging, tempfile
from pathlib import Path
from PIL import Image
import pytesseract

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_config():
    cfg_path = Path("config.json")
    data = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except Exception as e:
            logger.error("No pude leer config.json: %s", e)
    return {
        "CODE_AI_API_URL": os.environ.get("CODE_AI_API_URL", data.get("CODE_AI_API_URL")),
        "CODE_AI_API_KEY": os.environ.get("CODE_AI_API_KEY", data.get("CODE_AI_API_KEY")),
        "BOT_TOKEN": os.environ.get("BOT_TOKEN", data.get("BOT_TOKEN")),
        "TWILIO_ACCOUNT_SID": os.environ.get("TWILIO_ACCOUNT_SID", data.get("TWILIO_ACCOUNT_SID")),
        "TWILIO_AUTH_TOKEN": os.environ.get("TWILIO_AUTH_TOKEN", data.get("TWILIO_AUTH_TOKEN")),
        "TWILIO_WHATSAPP_NUMBER": os.environ.get("TWILIO_WHATSAPP_NUMBER", data.get("TWILIO_WHATSAPP_NUMBER")),
    }

CFG = load_config()
AI_URL = CFG["CODE_AI_API_URL"]
AI_KEY = CFG["CODE_AI_API_KEY"]

def analyze_code(text: str) -> str:
    if not AI_URL or not AI_KEY:
        return "[Config faltante] Define CODE_AI_API_URL y CODE_AI_API_KEY (en config.json o env)."
    try:
        resp = requests.post(
            AI_URL,
            headers={
                "Authorization": f"Bearer {AI_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Eres un analizador de errores de código. "
                            "Identifica el fallo, explica la causa y da pasos concretos para corregirlo."
                        )
                    },
                    {
                        "role": "user",
                        "content": f"Ayúdame con este error o código:\n{text}"
                    }
                ],
                "max_tokens": 400,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Error llamando al modelo: %s", e)
        return f"Hubo un problema al llamar al modelo: {e}"

def ocr_image_path(path: str) -> str:
    try:
        return pytesseract.image_to_string(Image.open(path))
    except Exception as e:
        logger.error("OCR falló: %s", e)
        return ""

def ocr_image_url(url: str) -> str:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(r.content)
            tmp.flush()
            return ocr_image_path(tmp.name)
    except Exception as e:
        logger.error("OCR URL falló: %s", e)
        return ""
