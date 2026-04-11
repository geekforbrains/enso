# Task 08: Slack File Upload/Download Handling

**Status:** Done
**Priority:** Low (can be added after core functionality works)
**Depends on:** Task 03

## Description

The Telegram transport supports file uploads (documents, photos, audio, video, voice) up to 20MB. Add equivalent support for Slack.

## Slack File Handling

### Receiving Files
Slack messages with files include a `files` array in the event payload:
```python
event["files"][0]["url_private_download"]  # requires bot token auth
event["files"][0]["name"]
event["files"][0]["mimetype"]
event["files"][0]["size"]
```

Download with:
```python
response = client.api_call(
    "files.info", params={"file": file_id}
)
# Or use requests with Authorization: Bearer xoxb-... header
```

### Sending Files
```python
client.files_upload_v2(
    channel=channel_id,
    thread_ts=thread_ts,
    file=file_path,
    title="filename.ext",
)
```

## Implementation

Mirror the Telegram transport's `_handle_file_message()` pattern:
1. Detect files in the Slack event
2. Download to `uploads/` directory
3. Build a prompt like `"User uploaded a file: /path/to/file.ext"`
4. Dispatch to runtime

## Acceptance Criteria

- [ ] Files shared in Slack DMs are downloaded and passed to the agent
- [ ] File size limit is enforced (20MB to match Telegram, or Slack's own limits)
- [ ] Caption/message text accompanying the file is included in the prompt
