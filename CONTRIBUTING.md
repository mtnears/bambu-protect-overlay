# Contributing

Thanks for your interest in improving Bambu Protect Overlay! Contributions are welcome.

## Reporting bugs

Open a [GitHub Issue](https://github.com/mtnears/bambu-protect-overlay/issues) with:

- **What you expected** vs **what actually happened**
- **Steps to reproduce**
- **Sanitized config files** (redact your access codes and serial numbers)
- **Container logs** from the relevant service:
  ```bash
  docker logs --tail 100 bambu-overlay
  docker logs --tail 100 go2rtc
  docker logs --tail 100 bambu-onvif
  ```
- **Bambu printer model** and firmware version
- **UniFi Protect version** and NVR hardware model

## Suggesting features

Open an issue describing:

- The use case
- Expected behavior
- Whether it should be opt-in or default
- Any compatibility concerns with existing setups

## Submitting code

1. Fork the repository
2. Create a feature branch (`git checkout -b feat/your-feature`)
3. Follow the existing code style:
   - Python: type hints where straightforward, helpful comments for non-obvious logic
   - YAML: comments explaining "why" not just "what"
   - Markdown: short paragraphs, code fences with language tags
4. Test thoroughly:
   - Confirm `docker compose up --build` works from a clean clone
   - Verify the overlay renders correctly with a real printer if possible
   - Check that CPU usage is still reasonable
5. Update relevant docs (README, TROUBLESHOOTING, etc.)
6. Submit a pull request with a clear description of what changed and why

## Sharing configurations for other Bambu models

The codebase was developed against the Bambu H2S. Other models (X1C, X1E, P1S, A1, A1 Mini) may have schema differences in the MQTT JSON. If your model needs schema tweaks:

1. Capture a real MQTT message (see [docs/CUSTOMIZING.md](docs/CUSTOMIZING.md)). **Sanitize anything sensitive.**
2. Open an issue with the message body and the field paths you found
3. Or open a PR adding the schema variation directly to `update_state()` in `bambu_overlay.py`

Particularly valuable contributions:
- P1S/A1 schema support (they use a different stream protocol entirely — would need a different upstream pull, not just parser changes)
- Multi-AMS unit support (current code picks the first unit; users with multiple AMS hardware would need true active-unit detection)
- Hardware acceleration profiles for QuickSync, NVENC, VAAPI

## Sharing your overlay layouts

If you've built a different overlay layout (different fields, different positioning, different styling) and want to share it, open a PR with:

- Your modified `bambu_overlay.py` (or a config-driven layout system if you've made it data-driven)
- A screenshot of the result in Protect
- A note on what tradeoff your layout optimizes for (info density vs readability, etc.)

## Code of conduct

Be respectful, helpful, and patient with others. This is a hobby project and contributions happen on hobby time. Help others when you can.
