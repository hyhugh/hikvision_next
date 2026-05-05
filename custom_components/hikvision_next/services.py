import asyncio
from contextlib import suppress
import datetime
import logging
import os
import shutil

from httpx import HTTPStatusError
import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    ACTION_EXTRACT_NVR_CLIP,
    ACTION_EXTRACT_NVR_CLIP_BY_TIME,
    ACTION_ISAPI_REQUEST,
    ACTION_REBOOT,
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
)
from .isapi import ISAPIForbiddenError, ISAPIUnauthorizedError

_LOGGER = logging.getLogger(__name__)

ACTION_ISAPI_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): str,
        vol.Required("method"): str,
        vol.Required("path"): str,
        vol.Optional("payload"): str,
    }
)

ACTION_EXTRACT_NVR_CLIP_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("filename"): cv.string,
        vol.Optional("duration", default=30): vol.Coerce(int),
        vol.Optional("lookback", default=10): vol.Coerce(int),
    }
)

ACTION_EXTRACT_NVR_CLIP_BY_TIME_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("filename"): cv.string,
        vol.Required("start_time"): cv.datetime,
        vol.Required("end_time"): cv.datetime,
        vol.Optional("pre_buffer", default=10): vol.Coerce(int),
        vol.Optional("post_buffer", default=20): vol.Coerce(int),
    }
)


def setup_services(hass: HomeAssistant) -> None:
    """Set up the services for the Hikvision component."""
    _extract_lock = asyncio.Lock()

    async def handle_reboot(call: ServiceCall):
        """Handle the reboot action call."""
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entry = hass.config_entries.async_get_entry(entry_id)
        device = entry.runtime_data
        try:
            await device.reboot()
        except (HTTPStatusError, ISAPIForbiddenError, ISAPIUnauthorizedError) as ex:
            raise HomeAssistantError(ex.response.content) from ex

    async def handle_isapi_request(call: ServiceCall) -> ServiceResponse:
        """Handle the custom ISAPI request action call."""
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entry = hass.config_entries.async_get_entry(entry_id)
        device = entry.runtime_data
        method = call.data.get("method", "POST")
        path = call.data["path"].strip("/")
        payload = call.data.get("payload")
        try:
            response = await device.request(method, path, present="xml", data=payload)
        except (HTTPStatusError, ISAPIForbiddenError, ISAPIUnauthorizedError) as ex:
            if isinstance(ex.response.content, bytes):
                response = ex.response.content.decode("utf-8")
            else:
                response = ex.response.content
        return {"data": response.replace("\r", "")}

    async def handle_extract_nvr_clip(call: ServiceCall):
        """Handle the extract NVR clip action call."""
        try:
            from homeassistant.helpers import service
            import inspect
            
            # Handle backward compatibility for async_extract_entity_ids
            # In newer HA versions, the hass parameter is removed.
            sig = inspect.signature(service.async_extract_entity_ids)
            if "service_call" in sig.parameters and "hass" not in sig.parameters:
                entity_ids = await service.async_extract_entity_ids(call)
            else:
                try:
                    # Try new signature first (if hass is deprecated but still accepted as kwarg or similar)
                    entity_ids = await service.async_extract_entity_ids(call)
                except TypeError:
                    # Fallback to old signature
                    entity_ids = await service.async_extract_entity_ids(hass, call)
                    
            if not entity_ids:
                raise HomeAssistantError("No entity_id provided in target.")
            entity_id = next(iter(entity_ids))

            filename = call.data["filename"]
            duration = call.data["duration"]
            lookback = call.data["lookback"]
            
            if duration > 600:
                _LOGGER.warning("Requested duration (%s) exceeds maximum limit of 600 seconds. Clipping to 600 seconds.", duration)
                duration = 600

            entity_registry = er.async_get(hass)
            entry = entity_registry.async_get(entity_id)
            if not entry:
                raise HomeAssistantError(f"Entity {entity_id} not found in registry")

            config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
            if not config_entry:
                raise HomeAssistantError(f"Config entry for {entity_id} not found")

            device = config_entry.runtime_data

            # Find the stream info matching this entity
            stream_info = None
            for camera in device.cameras:
                for stream in camera.streams:
                    unique_id = slugify(f"{device.device_info.serial_no.lower()}_{stream.id}")
                    if unique_id == entry.unique_id:
                        stream_info = stream
                        break
                if stream_info:
                    break

            if not stream_info:
                raise HomeAssistantError(f"Could not find stream info for {entity_id}")

            # Calculate local timestamps (Hikvision NVRs often expect local time digits despite the Z suffix)
            trigger_time = dt_util.now()
            start_dt = trigger_time - datetime.timedelta(seconds=lookback)
            end_dt = start_dt + datetime.timedelta(seconds=duration)

            start_time = start_dt.strftime("%Y%m%dT%H%M%SZ")
            end_time = end_dt.strftime("%Y%m%dT%H%M%SZ")

            # Constructed URL with explicit encoding and versioning for debugging
            from urllib.parse import quote, urlencode
            u = quote(device.username, safe="")
            p = quote(device.password, safe="")
            
            params = {
                "starttime": start_time,
                "endtime": end_time
            }
            query_string = urlencode(params)
            
            base_url = "rtsp://{}:{}@{}:{}/Streaming/tracks/{}".format(
                u, p, device.device_info.ip_address, device.protocols.rtsp_port, stream_info.id
            )
            playback_url = f"{base_url}?{query_string}"

            # Ensure directory exists
            dirname = os.path.dirname(filename)
            if dirname and not os.path.exists(dirname):
                _LOGGER.debug("Creating directory %s", dirname)
                os.makedirs(dirname, exist_ok=True)

            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                raise HomeAssistantError("ffmpeg binary not found")

            # Use a temporary file to avoid race conditions with background uploaders (like rclone)
            tmp_filename = f"{filename}.tmp"

            # Run ffmpeg in background
            args = [
                ffmpeg_bin,
                "-y",
                "-rtsp_transport",
                "tcp",
                "-rtsp_flags",
                "prefer_tcp",
                "-timeout",
                "10000000",  # 10 second socket timeout in microseconds
                "-i",
                playback_url,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-t",
                str(duration),
                "-f",
                "mp4",
                tmp_filename,
            ]

            _LOGGER.debug("Starting NVR clip extraction with arguments: %s", args)

            async def wait_for_process(proc, entity_id, filename, tmp_filename, max_wait):
                """Wait for the process to finish and log the result."""
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max_wait)
                except asyncio.TimeoutError:
                    _LOGGER.error("NVR clip extraction for %s timed out after %s seconds. Force killing FFmpeg.", entity_id, max_wait)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    # Cleanup tmp file on timeout
                    if os.path.exists(tmp_filename):
                        with suppress(OSError):
                            os.remove(tmp_filename)
                    return False

                returncode = proc.returncode
                
                # Check if file exists and has size
                file_size = os.path.getsize(tmp_filename) if os.path.exists(tmp_filename) else 0
                
                if returncode == 0 and file_size > 1000: # Greater than 1KB
                    # Atomically rename the tmp file to the final filename
                    try:
                        os.rename(tmp_filename, filename)
                        _LOGGER.info(
                            "Successfully finished NVR clip extraction for %s to %s (Size: %s bytes)",
                            entity_id,
                            filename,
                            file_size
                        )
                        return True
                    except OSError as err:
                        _LOGGER.error("Failed to rename temporary clip file %s to %s: %s", tmp_filename, filename, err)
                        return False
                else:
                    error_msg = stderr.decode().strip() if stderr else "No error message captured"
                    status_msg = "failed" if returncode != 0 else "produced an empty file"
                    _LOGGER.error(
                        "NVR clip extraction %s for %s. Return code: %s, File size: %s bytes. Error: %s",
                        status_msg,
                        entity_id,
                        returncode,
                        file_size,
                        error_msg,
                    )
                    # Cleanup empty/failed tmp file
                    if os.path.exists(tmp_filename):
                        with suppress(OSError):
                            os.remove(tmp_filename)
                    return False

            async def run_extraction_task():
                async with _extract_lock:
                    max_retries = 3
                    for attempt in range(max_retries):
                        # Start process
                        process = await asyncio.create_subprocess_exec(
                            *args,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        
                        if attempt == 0:
                            _LOGGER.info("Started NVR clip extraction for %s to %s", entity_id, filename)
                        else:
                            _LOGGER.info("Retrying NVR clip extraction (Attempt %s/%s) for %s", attempt + 1, max_retries, entity_id)
                            
                        # Allow duration + 45 seconds buffer for NVR throttling
                        max_wait_time = (duration * 2) + 120
                        success = await wait_for_process(process, entity_id, filename, tmp_filename, max_wait_time)
                        
                        if success:
                            break
                            
                        if attempt < max_retries - 1:
                            # Wait before retrying to let the NVR recover its stream buffers
                            await asyncio.sleep(5)
                    else:
                        _LOGGER.error("NVR clip extraction ultimately failed for %s after %s attempts.", entity_id, max_retries)

            hass.async_create_task(run_extraction_task())

        except Exception as ex:
            _LOGGER.error("Fatal error in extract_nvr_clip service: %s", ex, exc_info=True)
            raise HomeAssistantError(f"Failed to extract NVR clip: {ex}") from ex


    async def handle_extract_nvr_clip_by_time(call: ServiceCall):
        """Handle the extract NVR clip by time action call."""
        try:
            from homeassistant.helpers import service
            import inspect
            
            sig = inspect.signature(service.async_extract_entity_ids)
            if "service_call" in sig.parameters and "hass" not in sig.parameters:
                entity_ids = await service.async_extract_entity_ids(call)
            else:
                try:
                    entity_ids = await service.async_extract_entity_ids(call)
                except TypeError:
                    entity_ids = await service.async_extract_entity_ids(hass, call)
                    
            if not entity_ids:
                raise HomeAssistantError("No entity_id provided in target.")
            entity_id = next(iter(entity_ids))

            filename = call.data["filename"]
            start_time_raw = call.data["start_time"]
            end_time_raw = call.data["end_time"]
            pre_buffer = call.data["pre_buffer"]
            post_buffer = call.data["post_buffer"]

            entity_registry = er.async_get(hass)
            entry = entity_registry.async_get(entity_id)
            if not entry:
                raise HomeAssistantError(f"Entity {entity_id} not found in registry")

            config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
            if not config_entry:
                raise HomeAssistantError(f"Config entry for {entity_id} not found")

            device = config_entry.runtime_data

            stream_info = None
            for camera in device.cameras:
                for stream in camera.streams:
                    unique_id = slugify(f"{device.device_info.serial_no.lower()}_{stream.id}")
                    if unique_id == entry.unique_id:
                        stream_info = stream
                        break
                if stream_info:
                    break

            if not stream_info:
                raise HomeAssistantError(f"Could not find stream info for {entity_id}")

            start_dt = dt_util.as_local(start_time_raw) - datetime.timedelta(seconds=pre_buffer)
            end_dt = dt_util.as_local(end_time_raw) + datetime.timedelta(seconds=post_buffer)
            duration = int((end_dt - start_dt).total_seconds())
            
            if duration <= 0:
                raise HomeAssistantError("Calculated duration is less than or equal to 0 seconds")
            if duration > 600:
                _LOGGER.warning("Calculated duration (%s) exceeds maximum limit of 600 seconds. Clipping to 600 seconds.", duration)
                duration = 600
                end_dt = start_dt + datetime.timedelta(seconds=duration)

            start_time = start_dt.strftime("%Y%m%dT%H%M%SZ")
            end_time = end_dt.strftime("%Y%m%dT%H%M%SZ")

            from urllib.parse import quote, urlencode
            u = quote(device.username, safe="")
            p = quote(device.password, safe="")
            
            params = {
                "starttime": start_time,
                "endtime": end_time
            }
            query_string = urlencode(params)
            
            base_url = "rtsp://{}:{}@{}:{}/Streaming/tracks/{}".format(
                u, p, device.device_info.ip_address, device.protocols.rtsp_port, stream_info.id
            )
            playback_url = f"{base_url}?{query_string}"

            dirname = os.path.dirname(filename)
            if dirname and not os.path.exists(dirname):
                os.makedirs(dirname, exist_ok=True)

            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                raise HomeAssistantError("ffmpeg binary not found")

            tmp_filename = f"{filename}.tmp"

            args = [
                ffmpeg_bin,
                "-y",
                "-rtsp_transport",
                "tcp",
                "-rtsp_flags",
                "prefer_tcp",
                "-timeout",
                "10000000",
                "-i",
                playback_url,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-t",
                str(duration),
                "-f",
                "mp4",
                tmp_filename,
            ]

            async def wait_for_process(proc, entity_id, filename, tmp_filename, max_wait):
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=max_wait)
                except asyncio.TimeoutError:
                    _LOGGER.error("NVR clip extraction for %s timed out after %s seconds. Force killing FFmpeg.", entity_id, max_wait)
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    if os.path.exists(tmp_filename):
                        with suppress(OSError):
                            os.remove(tmp_filename)
                    return False

                returncode = proc.returncode
                file_size = os.path.getsize(tmp_filename) if os.path.exists(tmp_filename) else 0
                
                if returncode == 0 and file_size > 1000:
                    try:
                        os.rename(tmp_filename, filename)
                        _LOGGER.info("Successfully finished NVR clip extraction for %s to %s (Size: %s bytes)", entity_id, filename, file_size)
                        return True
                    except OSError as err:
                        _LOGGER.error("Failed to rename temporary clip file %s to %s: %s", tmp_filename, filename, err)
                        return False
                else:
                    error_msg = stderr.decode().strip() if stderr else "No error message captured"
                    status_msg = "failed" if returncode != 0 else "produced an empty file"
                    _LOGGER.error("NVR clip extraction %s for %s. Return code: %s, File size: %s bytes. Error: %s", status_msg, entity_id, returncode, file_size, error_msg)
                    if os.path.exists(tmp_filename):
                        with suppress(OSError):
                            os.remove(tmp_filename)
                    return False

            async def run_extraction_task():
                async with _extract_lock:
                    max_retries = 3
                    for attempt in range(max_retries):
                        process = await asyncio.create_subprocess_exec(
                            *args,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        if attempt == 0:
                            _LOGGER.info("Started NVR clip extraction for %s to %s", entity_id, filename)
                        else:
                            _LOGGER.info("Retrying NVR clip extraction (Attempt %s/%s) for %s", attempt + 1, max_retries, entity_id)
                            
                        max_wait_time = (duration * 2) + 120
                        success = await wait_for_process(process, entity_id, filename, tmp_filename, max_wait_time)
                        
                        if success:
                            break
                            
                        if attempt < max_retries - 1:
                            await asyncio.sleep(5)
                    else:
                        _LOGGER.error("NVR clip extraction ultimately failed for %s after %s attempts.", entity_id, max_retries)

            hass.async_create_task(run_extraction_task())

        except Exception as ex:
            _LOGGER.error("Fatal error in extract_nvr_clip_by_time service: %s", ex, exc_info=True)
            raise HomeAssistantError(f"Failed to extract NVR clip: {ex}") from ex

    hass.services.async_register(
        DOMAIN,
        ACTION_REBOOT,
        handle_reboot,
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_ISAPI_REQUEST,
        handle_isapi_request,
        schema=ACTION_ISAPI_REQUEST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        ACTION_EXTRACT_NVR_CLIP,
        handle_extract_nvr_clip,
        schema=ACTION_EXTRACT_NVR_CLIP_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        ACTION_EXTRACT_NVR_CLIP_BY_TIME,
        handle_extract_nvr_clip_by_time,
        schema=ACTION_EXTRACT_NVR_CLIP_BY_TIME_SCHEMA,
    )
