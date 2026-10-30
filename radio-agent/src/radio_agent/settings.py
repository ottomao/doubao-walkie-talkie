from dataclasses import dataclass, field
from pathlib import Path
import math
import os

from dotenv import dotenv_values


@dataclass
class Settings:
    api_key: str = field(default='', repr=False)
    app_id: str = field(default='', repr=False)
    access_key: str = field(default='', repr=False)
    model: str = '1.2.1.1'
    speaker: str = 'zh_male_yunzhou_jupiter_bigtts'
    tx_gain: float = 0.75
    silence_seconds: float = 1
    vad_min_dbfs: float = -42
    max_input_seconds: float = 30
    max_reply_seconds: float = 20
    reply_timeout_seconds: float = 45
    idle_reset_seconds: float = 300
    guard_seconds: float = 0.4
    log_dir: Path = Path('data')
    endpoint: str = 'wss://openspeech.bytedance.com/api/v3/realtime/dialogue'

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        env = {**(dotenv_values(path) if path.exists() else {}), **os.environ}
        args = {}
        for key in ('api_key', 'app_id', 'access_key', 'model', 'speaker'):
            value = env.get('VOLC_' + key.upper())
            if value:
                args[key] = value
        for key in ('tx_gain', 'silence_seconds', 'vad_min_dbfs', 'max_input_seconds',
                    'max_reply_seconds', 'reply_timeout_seconds', 'idle_reset_seconds', 'guard_seconds'):
            value = env.get('RADIO_' + key.upper())
            if value:
                args[key] = float(value)
        args['log_dir'] = path.parent / env.get('RADIO_LOG_DIR', 'data')
        result = cls(**args)
        result.validate()
        return result

    def validate(self):
        limits = dict(tx_gain=(0.01, 1), silence_seconds=(0.5, 3), vad_min_dbfs=(-80, -10),
                      max_input_seconds=(2, 60), max_reply_seconds=(1, 55),
                      reply_timeout_seconds=(5, 120), idle_reset_seconds=(10, 600), guard_seconds=(0.1, 3))
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{name} must be between {low} and {high}')

    def require_credentials(self):
        if not (self.api_key or (self.app_id and self.access_key)):
            raise ValueError('Missing credentials: fill VOLC_API_KEY or VOLC_APP_ID + VOLC_ACCESS_KEY in radio-agent/.env')

    def public(self):
        return {k: str(v) if isinstance(v, Path) else v for k, v in vars(self).items()
                if k not in ('api_key', 'app_id', 'access_key')}

    def redact(self, value):
        text = str(value)
        for secret in (self.api_key, self.app_id, self.access_key):
            if secret:
                text = text.replace(secret, '[REDACTED]')
        return text
