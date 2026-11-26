"""Configuracion. Todo sale del entorno; ningun secreto tiene valor por defecto."""
import os
import re
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on", "si"}


def digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def same_number(a, b) -> bool:
    """Compara por los ultimos 10 digitos: Mexico llega como 52 o como 521."""
    a, b = digits(a), digits(b)
    return len(a) >= 10 and len(b) >= 10 and a[-10:] == b[-10:]


@dataclass
class Config:
    enabled: bool = False
    meta_token: str = field(default="", repr=False)
    phone_number_id: str = ""
    verify_token: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    graph_version: str = ""
    bot_number: str = ""
    kv_url: str = field(default="", repr=False)
    advisor_number: str = ""
    advisor_template: str = ""
    advisor_template_lang: str = "es_MX"
    admin_token: str = field(default="", repr=False)
    sheet_id: str = ""
    sheet_tab: str = "Prospectos GastroRadar"
    google_credentials: str = field(default="", repr=False)
    ai_enabled: bool = False
    openai_key: str = field(default="", repr=False)
    openai_model: str = "gpt-4o-mini"
    ai_timeout: float = 12.0
    ai_max_input_chars: int = 600
    ai_max_output_tokens: int = 300
    ai_user_daily: int = 15
    ai_global_daily: int = 300
    user_msgs_per_10min: int = 20
    tpv_benefit_verified: bool = False
    online_contracting: bool = False
    worker_threads: int = 3

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def s(name, default=""):
            return str(env.get(name, "") or "").strip() or default

        def b(name):
            return s(name).lower() in _TRUE

        def n(name, default, cast=int):
            try:
                return cast(s(name, str(default)))
            except ValueError:
                return default

        return cls(
            enabled=b("GASTRORADAR_ENABLED"),
            meta_token=s("META_TOKEN"),
            phone_number_id=s("WABA_PHONE_ID"),
            verify_token=s("VERIFY_TOKEN"),
            app_secret=s("META_APP_SECRET"),
            graph_version=s("META_GRAPH_VERSION"),
            bot_number=digits(s("BOT_WHATSAPP_NUMBER")),
            kv_url=s("KV_URL") or s("REDIS_URL"),
            advisor_number=digits(s("ADVISOR_NUMBER")),
            advisor_template=s("ADVISOR_TEMPLATE_NAME"),
            advisor_template_lang=s("ADVISOR_TEMPLATE_LANG", "es_MX"),
            admin_token=s("ADMIN_TOKEN"),
            sheet_id=s("SHEETS_ID_PROSPECTOS"),
            sheet_tab=s("SHEETS_TAB_PROSPECTOS", "Prospectos GastroRadar"),
            google_credentials=s("GOOGLE_CREDENTIALS_JSON"),
            ai_enabled=b("AI_ENABLED"),
            openai_key=s("OPENAI_API_KEY"),
            openai_model=s("OPENAI_MODEL", "gpt-4o-mini"),
            ai_timeout=n("AI_TIMEOUT_SECONDS", 12.0, float),
            ai_max_input_chars=n("AI_MAX_INPUT_CHARS", 600),
            ai_max_output_tokens=n("AI_MAX_OUTPUT_TOKENS", 300),
            ai_user_daily=n("AI_USER_DAILY_LIMIT", 15),
            ai_global_daily=n("AI_GLOBAL_DAILY_LIMIT", 300),
            user_msgs_per_10min=n("USER_MSGS_PER_10MIN", 20),
            tpv_benefit_verified=b("TPV_BENEFIT_VERIFIED"),
            online_contracting=b("ONLINE_CONTRACTING_AVAILABLE"),
            worker_threads=n("WORKER_THREADS", 3),
        )

    def advisor_problem(self) -> str:
        """Motivo por el que no se puede avisar al asesor ('' si todo bien)."""
        if not self.advisor_number:
            return "sin_numero_asesor"
        if not self.bot_number:
            # Sin el numero propio no se puede descartar un bucle.
            return "sin_numero_del_bot"
        if same_number(self.advisor_number, self.bot_number):
            return "asesor_es_el_bot"
        return ""

    def missing_required(self) -> list:
        required = {
            "META_TOKEN": self.meta_token, "WABA_PHONE_ID": self.phone_number_id,
            "VERIFY_TOKEN": self.verify_token, "META_APP_SECRET": self.app_secret,
            "META_GRAPH_VERSION": self.graph_version, "KV_URL": self.kv_url,
            "BOT_WHATSAPP_NUMBER": self.bot_number,
        }
        return [k for k, v in required.items() if not v]
