"""Lightweight mobile client state for the API server gateway."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_GROUP_ID = "default"
PAIRING_TTL_SECONDS = 600


class MobileStoreError(Exception):
    """Typed store error that HTTP handlers can translate into JSON responses."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class MobileStore:
    """Small JSON-backed store for iOS pairing, devices, conversations, and messages."""

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        self._path: Optional[Path]
        if path == ":memory:":
            self._path = None
        elif path:
            self._path = Path(path).expanduser()
        else:
            try:
                from hermes_cli.config import get_hermes_home

                self._path = get_hermes_home() / "mobile_store.json"
            except Exception:
                self._path = None
        self._data = self._load()
        self._ensure_defaults()

    def _load(self) -> Dict[str, Any]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def _ensure_defaults(self) -> None:
        with self._lock:
            self._data.setdefault("pairings", {})
            self._data.setdefault("devices", {})
            self._data.setdefault("tokens", {})
            self._data.setdefault("registration_codes", {})
            self._data.setdefault("conversations", {})
            self._data.setdefault("messages", {})
            self._data["conversations"].setdefault(
                DEFAULT_GROUP_ID,
                {
                    "conversation_id": DEFAULT_GROUP_ID,
                    "type": "group",
                    "title": "Default Group",
                    "members": [],
                    "created_at": _utc_iso(),
                    "updated_at": _utc_iso(),
                },
            )
            self._save()

    def create_pairing(self, registration_url: str, ttl_seconds: int = PAIRING_TTL_SECONDS) -> Dict[str, Any]:
        now = time.time()
        pairing = {
            "pairing_id": _new_id("pairing"),
            "registration_url": registration_url,
            "verification_code": f"{secrets.randbelow(1_000_000):06d}",
            "expires_at": _utc_iso(now + ttl_seconds),
            "expires_at_ts": now + ttl_seconds,
            "created_at": _utc_iso(now),
            "used_at": None,
            "device_id": None,
        }
        with self._lock:
            self._data["pairings"][pairing["pairing_id"]] = pairing
            self._save()
        return dict(pairing)

    def expire_pairing(self, pairing_id: str) -> None:
        with self._lock:
            pairing = self._data["pairings"].get(pairing_id)
            if pairing:
                pairing["expires_at_ts"] = time.time() - 1
                pairing["expires_at"] = _utc_iso(pairing["expires_at_ts"])
                self._save()

    def register_device(
        self,
        *,
        pairing_id: str,
        verification_code: str,
        registration_code: str,
        device_name: str,
    ) -> Dict[str, Any]:
        now = time.time()
        registration_code = str(registration_code or "").strip()
        verification_code = str(verification_code or "").strip()
        device_name = str(device_name or "").strip() or "iPhone"
        if not registration_code:
            raise MobileStoreError("missing_registration_code", "registration_code is required")

        with self._lock:
            pairing = self._data["pairings"].get(pairing_id)
            if not pairing:
                raise MobileStoreError("pairing_not_found", "Pairing not found", status=404)
            if pairing.get("used_at"):
                raise MobileStoreError("pairing_used", "Pairing has already been used", status=409)
            if float(pairing.get("expires_at_ts") or 0) < now:
                raise MobileStoreError("pairing_expired", "Pairing has expired", status=410)
            if not secrets.compare_digest(str(pairing.get("verification_code", "")), verification_code):
                raise MobileStoreError("invalid_verification_code", "Verification code does not match", status=400)
            if registration_code in self._data["registration_codes"]:
                raise MobileStoreError("duplicate_registration_code", "Device registration code is already registered", status=409)

            device_id = _new_id("dev")
            device_token = secrets.token_urlsafe(32)
            device = {
                "device_id": device_id,
                "device_token": device_token,
                "registration_code": registration_code,
                "device_name": device_name,
                "created_at": _utc_iso(now),
                "updated_at": _utc_iso(now),
                "last_seen_at": None,
                "online": False,
            }
            self._data["devices"][device_id] = device
            self._data["tokens"][device_token] = device_id
            self._data["registration_codes"][registration_code] = device_id
            pairing["used_at"] = _utc_iso(now)
            pairing["device_id"] = device_id

            default_group = self._data["conversations"][DEFAULT_GROUP_ID]
            if device_id not in default_group["members"]:
                default_group["members"].append(device_id)
            default_group["updated_at"] = _utc_iso(now)

            hermes_conversation_id = self.hermes_conversation_id(device_id)
            self._data["conversations"].setdefault(
                hermes_conversation_id,
                {
                    "conversation_id": hermes_conversation_id,
                    "type": "hermes",
                    "title": "Hermes",
                    "members": [device_id, "hermes"],
                    "created_at": _utc_iso(now),
                    "updated_at": _utc_iso(now),
                },
            )
            self._save()
            return dict(device)

    def authenticate_device(self, token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            device_id = self._data["tokens"].get(str(token or ""))
            if not device_id:
                return None
            device = self._data["devices"].get(device_id)
            return dict(device) if device else None

    def set_online(self, device_id: str, online: bool) -> Optional[Dict[str, Any]]:
        with self._lock:
            device = self._data["devices"].get(device_id)
            if not device:
                return None
            now = time.time()
            device["online"] = bool(online)
            device["last_seen_at"] = _utc_iso(now)
            device["updated_at"] = _utc_iso(now)
            self._save()
            return self.public_device(device_id)

    def touch(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            device = self._data["devices"].get(device_id)
            if not device:
                return None
            device["last_seen_at"] = _utc_iso()
            device["updated_at"] = _utc_iso()
            self._save()
            return self.public_device(device_id)

    def public_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        device = self._data["devices"].get(device_id)
        if not device:
            return None
        return {
            "device_id": device["device_id"],
            "device_name": device.get("device_name") or "iPhone",
            "online": bool(device.get("online")),
            "last_seen_at": device.get("last_seen_at"),
        }

    def hermes_conversation_id(self, device_id: str) -> str:
        return f"hermes:{device_id}"

    def conversation_exists_for_device(self, conversation_id: str, device_id: str) -> bool:
        with self._lock:
            convo = self._data["conversations"].get(conversation_id)
            return bool(convo and device_id in convo.get("members", []))

    def list_members(self, conversation_id: str = DEFAULT_GROUP_ID) -> List[Dict[str, Any]]:
        with self._lock:
            convo = self._data["conversations"].get(conversation_id) or {}
            members = []
            for member_id in convo.get("members", []):
                if member_id == "hermes":
                    members.append({"device_id": "hermes", "device_name": "Hermes", "online": True})
                    continue
                public = self.public_device(member_id)
                if public:
                    members.append(public)
            return members

    def list_conversations(self, device_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            result = []
            for convo in self._data["conversations"].values():
                if device_id not in convo.get("members", []):
                    continue
                conversation_id = convo["conversation_id"]
                result.append({
                    "conversation_id": conversation_id,
                    "type": convo.get("type", "group"),
                    "title": convo.get("title") or conversation_id,
                    "members": self.list_members(conversation_id),
                    "updated_at": convo.get("updated_at"),
                })
            return sorted(result, key=lambda item: (item["conversation_id"] != DEFAULT_GROUP_ID, item["conversation_id"]))

    def bootstrap(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            self.touch(device_id)
            device = self.public_device(device_id)
            return {
                "status": "registered",
                "device": device,
                "default_conversation": {
                    "conversation_id": DEFAULT_GROUP_ID,
                    "title": "Default Group",
                    "members": self.list_members(DEFAULT_GROUP_ID),
                },
                "conversations": self.list_conversations(device_id),
            }

    def create_message(
        self,
        *,
        conversation_id: str,
        sender_id: str,
        text: str,
        kind: str = "user",
        status: str = "sent",
        run_id: Optional[str] = None,
        receipts: Optional[List[Dict[str, Any]]] = None,
        retry_of: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _utc_iso()
        message = {
            "message_id": _new_id("msg"),
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "text": text,
            "kind": kind,
            "status": status,
            "run_id": run_id,
            "receipts": receipts or [],
            "retry_of": retry_of,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._data["messages"][message["message_id"]] = message
            convo = self._data["conversations"].get(conversation_id)
            if convo:
                convo["updated_at"] = now
            self._save()
            return dict(message)

    def update_message(self, message_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            message = self._data["messages"].get(message_id)
            if not message:
                return None
            message.update(fields)
            message["updated_at"] = _utc_iso()
            self._save()
            return dict(message)

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            message = self._data["messages"].get(message_id)
            return dict(message) if message else None

    def recent_messages(self, conversation_id: str, limit: int = 12) -> List[Dict[str, Any]]:
        """Return recent non-empty messages for a conversation in chronological order."""
        with self._lock:
            messages = [
                dict(message)
                for message in self._data["messages"].values()
                if message.get("conversation_id") == conversation_id and str(message.get("text") or "").strip()
            ]
            messages.sort(key=lambda item: str(item.get("created_at") or ""))
            return messages[-max(1, int(limit)):]

    def delivery_receipts_for_forward(self, sender_device_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            receipts = []
            for device_id in self._data["conversations"][DEFAULT_GROUP_ID].get("members", []):
                if device_id == sender_device_id:
                    continue
                device = self._data["devices"].get(device_id)
                if not device:
                    continue
                receipts.append({
                    "device_id": device_id,
                    "device_name": device.get("device_name") or "iPhone",
                    "status": "delivered" if device.get("online") else "pending_offline",
                    "updated_at": _utc_iso(),
                })
            return receipts
