# Security Notes

## Trust boundary

Video Reviewer is a local prototype. Flask binds to `127.0.0.1` and rejects non-local hosts. Do not expose port 5055 through port forwarding, a reverse proxy, Windows firewall rules, or LAN binding.

Any process running as the same Windows user can still access the local API and project files. This prototype does not provide user authentication.

## Internet use

Standard analysis is local. Optional visual research is disabled unless selected in the Visual check panel. When enabled, only identified subject names are sent to Wikipedia over HTTPS; video frames and scripts are not uploaded to Wikipedia. Ollama is expected at `127.0.0.1:11434`.

Research references are background evidence, not proof that footage is correct. Verify findings against the video.

## File and media safety

- Uploads are limited to approved video extensions and 4 GB.
- Uploaded media is passed to FFmpeg/OpenCV for decoding, not executed as a program.
- Project video paths are constrained to their project folder.
- Analysis JSON is written atomically.
- Storage cleanup accepts only server-generated candidate IDs and permanently deletes selected candidates. Active projects and bundled models are excluded.
- Keep your operating system and Python/package updates current. No application can guarantee safety from a compromised decoder, operating system, or dependency.

## Cleanup

The storage cleaner is destructive. Review each path and size before confirming. Keep project folders containing `meta.json`, `status.json`, `analysis.json`, `review.json`, or reports unless the project is no longer needed.
