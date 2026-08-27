"""TTS audio generation for insight sections using OpenAI and S3."""

import os
import re
import logging
from concurrent.futures import ThreadPoolExecutor

import boto3
from openai import OpenAI

logger = logging.getLogger(__name__)

s3_client = boto3.client('s3')

# Abbreviations to expand for natural-sounding TTS narration
_TTS_EXPANSIONS = [
    (re.compile(r'\be1RMs\b', re.IGNORECASE), 'estimated one rep maxes'),
    (re.compile(r'\be1RM\b', re.IGNORECASE), 'estimated one rep max'),
    (re.compile(r'\b1RMs\b', re.IGNORECASE), 'one rep maxes'),
    (re.compile(r'\b1RM\b', re.IGNORECASE), 'one rep max'),
]

# ISO date pattern: YYYY-MM-DD
_DATE_PATTERN = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')

_MONTH_NAMES = [
    '', 'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        return f"{day}th"
    return f"{day}{['th', 'st', 'nd', 'rd'][day % 10] if day % 10 < 4 else 'th'}"


def _expand_date(match: re.Match) -> str:
    month = int(match.group(2))
    day = int(match.group(3))
    if 1 <= month <= 12:
        return f"{_MONTH_NAMES[month]} {_ordinal(day)}"
    return match.group(0)


def _prepare_for_tts(text: str) -> str:
    """Expand abbreviations and dates so TTS reads them naturally."""
    for pattern, replacement in _TTS_EXPANSIONS:
        text = pattern.sub(replacement, text)
    text = _DATE_PATTERN.sub(_expand_date, text)
    return text


def _generate_one(client: OpenAI, section_body: str, bucket: str, s3_key: str) -> str:
    """Generate TTS audio for a single section and upload to S3."""
    tts_text = _prepare_for_tts(section_body)
    response = client.with_options(max_retries=2, timeout=15.0).audio.speech.create(
        model="tts-1",
        voice="ash",
        input=tts_text,
    )

    audio_bytes = response.read()

    s3_client.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=audio_bytes,
        ContentType="audio/mpeg",
    )

    logger.info(f"Uploaded TTS audio to s3://{bucket}/{s3_key}")
    return s3_key

