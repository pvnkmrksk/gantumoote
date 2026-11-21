# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Pavan Kumar Kaushik. Part of gantumoote, https://github.com/pvnkmrksk/gantumoote (see NOTICE).
"""Render the repository's self-hosting guide inside the private dashboard."""

from markdown import markdown

from .settings import PROJECT

GUIDES = {
    "raspberry-pi": "1-start-the-dashboard",
    "openhaystack": "apple-find-my",
    "hubble": "hubble",
    "devices": "history-keys-and-backups",
}


def guide_html(name):
    source = (PROJECT / "README.md").read_text()
    body = markdown(source, extensions=["fenced_code", "tables", "toc"])
    return f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gantumoote · Setup guide</title>
<style>
body{{font:16px/1.6 system-ui;background:#141119;color:#eee4f0;max-width:820px;margin:30px auto;padding:20px}}
a{{color:#ffb36a}} pre{{overflow:auto;background:#241b2c;padding:15px;border-radius:10px}}
th,td{{padding:.5rem;border-bottom:1px solid #61455c;text-align:left}}
</style>
<nav><a href="/">← Dashboard</a> · <a href="#{GUIDES[name]}">Go to this section ↓</a></nav>
{body}
</html>"""
