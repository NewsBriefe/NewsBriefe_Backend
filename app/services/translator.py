"""
Translation Service

Priority: DeepL (highest quality) → Google Translate → passthrough.
Automatically skips translation when source == target language.
Caches translations in the DB to avoid re-translating same content.

FIX #9:  DeepL's Python SDK is sync-only. All calls now wrapped in
         asyncio.to_thread() so the event loop is never blocked.
FIX #15: Added th (Thai) and vi (Vietnamese) to Google codes to match
         Flutter's full 20-language list.
"""
import asyncio
import httpx
import deepl
from app.core.config import get_settings
from app.core.logging import get_logger

settings = get_settings()
log = get_logger(__name__)

# DeepL language code mapping (ISO 639-1 → DeepL target codes)
_DEEPL_CODES: dict[str, str] = {
    "en": "EN-US",
    "ar": "AR",
    "fr": "FR",
    "es": "ES",
    "pt": "PT-BR",
    "de": "DE",
    "ru": "RU",
    "zh": "ZH",
    "tr": "TR",
    "it": "IT",
    "nl": "NL",
    "pl": "PL",
    "ja": "JA",
    "ko": "KO",
    "id": "ID",
}

# Languages DeepL does NOT support — fall back to Google
# FIX #15: th (Thai) and vi (Vietnamese) added
_DEEPL_UNSUPPORTED = {"am", "sw", "hi", "th", "vi"}

# Google Translate language codes (mostly same as ISO 639-1)
# FIX #15: th and vi added
_GOOGLE_CODES: dict[str, str] = {
    "am": "am",
    "sw": "sw",
    "hi": "hi",
    "th": "th",    # FIX #15: Thai
    "vi": "vi",    # FIX #15: Vietnamese
    "zh": "zh-CN",
    "pt": "pt",
}


class TranslationService:
    def __init__(self) -> None:
        self._deepl: deepl.Translator | None = None
        if settings.deepl_api_key:
            self._deepl = deepl.Translator(settings.deepl_api_key)

    async def translate(
        self,
        text: str,
        target_lang: str,
        source_lang: str = "en",
    ) -> str:
        if source_lang == target_lang or target_lang == "en":
            return text

        if not text.strip():
            return text

        if self._deepl and target_lang not in _DEEPL_UNSUPPORTED:
            result = await self._translate_deepl(text, target_lang)
            if result:
                return result

        result = await self._translate_google(text, target_lang)
        if result:
            return result

        log.warning("translation_failed", lang=target_lang, text_len=len(text))
        return text

    async def translate_pair(
        self,
        title: str,
        summary: str,
        target_lang: str,
    ) -> tuple[str, str]:
        if target_lang == "en":
            return title, summary
        t_title = await self.translate(title, target_lang)
        t_summary = await self.translate(summary, target_lang)
        return t_title, t_summary

    # ── Provider implementations ─────────────────────────────

    async def _translate_deepl(self, text: str, target_lang: str) -> str | None:
        """
        FIX #9: deepl.Translator.translate_text() is synchronous and blocks
        the event loop. Wrapped in asyncio.to_thread() for true async execution.
        """
        deepl_code = _DEEPL_CODES.get(target_lang)
        if not deepl_code or not self._deepl:
            return None
        try:
            # Run the blocking DeepL call in a thread pool
            result = await asyncio.to_thread(
                self._deepl.translate_text,
                text,
                target_lang=deepl_code,
                source_lang="EN",
                formality="prefer_less",
            )
            return str(result)
        except deepl.DeepLException as e:
            log.warning("deepl_error", error=str(e), lang=target_lang)
            return None

    async def _translate_google(self, text: str, target_lang: str) -> str | None:
        """
        Uses Google Translate via the free unofficial REST endpoint.
        NOTE: For production traffic, replace with the official
        Google Cloud Translation API (paid, but reliable and ToS-compliant).
        """
        google_code = _GOOGLE_CODES.get(target_lang, target_lang)
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": google_code,
            "dt": "t",
            "q": text,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
                parts = [item[0] for item in data[0] if item[0]]
                return "".join(parts)
        except Exception as e:
            log.warning("google_translate_error", error=str(e), lang=target_lang)
            return None

    @staticmethod
    def provider_name(target_lang: str) -> str:
        if target_lang in _DEEPL_UNSUPPORTED or target_lang not in _DEEPL_CODES:
            return "google"
        return "deepl"


# ── Supported language metadata ──────────────────────────────
# FIX #15: th (Thai) and vi (Vietnamese) added to match Flutter's 20 languages.
LANGUAGE_METADATA: list[dict] = [
    {"code": "en", "name": "English",    "native": "English"},
    {"code": "am", "name": "Amharic",    "native": "አማርኛ"},
    {"code": "ar", "name": "Arabic",     "native": "العربية"},
    {"code": "fr", "name": "French",     "native": "Français"},
    {"code": "es", "name": "Spanish",    "native": "Español"},
    {"code": "pt", "name": "Portuguese", "native": "Português"},
    {"code": "sw", "name": "Swahili",    "native": "Kiswahili"},
    {"code": "hi", "name": "Hindi",      "native": "हिन्दी"},
    {"code": "zh", "name": "Mandarin",   "native": "中文"},
    {"code": "id", "name": "Indonesian", "native": "Bahasa Indonesia"},
    {"code": "tr", "name": "Turkish",    "native": "Türkçe"},
    {"code": "de", "name": "German",     "native": "Deutsch"},
    {"code": "ru", "name": "Russian",    "native": "Русский"},
    {"code": "ja", "name": "Japanese",   "native": "日本語"},
    {"code": "ko", "name": "Korean",     "native": "한국어"},
    {"code": "it", "name": "Italian",    "native": "Italiano"},
    {"code": "nl", "name": "Dutch",      "native": "Nederlands"},
    {"code": "pl", "name": "Polish",     "native": "Polski"},
    {"code": "th", "name": "Thai",       "native": "ภาษาไทย"},   # FIX #15
    {"code": "vi", "name": "Vietnamese", "native": "Tiếng Việt"}, # FIX #15
]
