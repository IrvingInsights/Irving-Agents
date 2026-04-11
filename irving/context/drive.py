"""
irving/context/drive.py
────────────────────────
Google Drive context injection and output saving.
"""
import logging
from typing import Optional, Tuple

from irving.config import GOOGLE_SA_INFO, DRIVE_OUTPUT_FOLDER_ID

logger = logging.getLogger(__name__)


def _get_drive_service():
    if not GOOGLE_SA_INFO:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            GOOGLE_SA_INFO, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Error building Drive service: {e}")
        return None


def get_drive_context(query: str = "", max_files: int = 3) -> str:
    drive = _get_drive_service()
    if not drive:
        return ""
    try:
        search_q = "mimeType='application/vnd.google-apps.document' and trashed=false"
        if query:
            safe = query[:50].replace("'", "")
            search_q += f" and fullText contains '{safe}'"
        results = drive.files().list(
            q=search_q, pageSize=max_files,
            fields="files(id, name, modifiedTime)", orderBy="modifiedTime desc",
        ).execute()
        files = results.get("files", [])
        if not files:
            return ""
        parts = ["--- GOOGLE DRIVE CONTEXT ---\n"]
        for f in files:
            try:
                raw  = drive.files().export(fileId=f["id"], mimeType="text/plain").execute()
                text = (raw.decode("utf-8") if isinstance(raw, bytes) else raw)[:2000].strip()
                if text:
                    parts.append(f"[{f['name']}] (modified: {f['modifiedTime'][:10]})\n{text}\n")
            except Exception as ex:
                logger.warning(f"Could not export {f['name']}: {ex}")
        parts.append("---")
        return "\n".join(parts) if len(parts) > 2 else ""
    except Exception as e:
        logger.error(f"Error fetching Drive context: {e}")
        return ""


def save_to_drive(
    filename: str, content: str, folder_id: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Save content as a Google Doc. Returns (webViewLink, error_message)."""
    drive = _get_drive_service()
    if not drive:
        return None, "Google Drive is not configured"
    try:
        from googleapiclient.http import MediaInMemoryUpload
        target = folder_id or DRIVE_OUTPUT_FOLDER_ID
        meta   = {"name": filename, "mimeType": "application/vnd.google-apps.document"}
        if target:
            meta["parents"] = [target]
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)
        file  = drive.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
        return file.get("webViewLink"), None
    except Exception as e:
        logger.error(f"Error saving to Drive: {e}")
        return None, str(e)
