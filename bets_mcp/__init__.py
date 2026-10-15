"""Matched Betting Assistant: an MCP server (ChatGPT plugin) in front of the Assistant Intake API.

It is only an adapter. Every tool maps to one Assistant Intake endpoint on 127.0.0.1; it never
touches the betting database (it runs as its own user, which cannot read it). Candidates it
submits wait for the user's Confirm / Edit / Reject in /bets/imports.
"""
