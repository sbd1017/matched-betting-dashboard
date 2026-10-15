# Prompts for AI assistants

You don't need an AI to use this, but it makes setting it up and day to day use a lot easier. Copy one of
these, fill in the bits in [square brackets] and paste it in.

## 1. Getting it installed

Use this with an AI that can run commands on your computer or server, like a coding agent (Codex, Cursor
and so on).

```
Install the Matched Betting Dashboard from https://github.com/sbd1017/matched-betting-dashboard for me.

Where it should run: [this computer / a Raspberry Pi at 192.168.1.50 / a VPS I can SSH into]
How I want to open it: [only on this computer / from my phone too, at my own domain, e.g. bets.example.com]
Connect ChatGPT to it: [yes / no]
Currency: [GBP / EUR / USD / AUD / CAD / NZD]

Follow README.md and docs/OPERATIONS.md, plus docs/chatgpt-plugin.md if I want ChatGPT connected.
Use git clone so scripts/update.sh works later, keep my data in the var folder and set up the daily backup.
Don't open any ports on my router or change my firewall without asking me first.
When you're done, tell me the web address, where my login is, and how to update it later.
```

If you want it on your phone or connected to ChatGPT, it has to be reachable over the internet with HTTPS.
I use a Cloudflare Tunnel for that, which means nothing on your router has to be opened.

## 2. Your first message once ChatGPT is connected

When ChatGPT connects, the dashboard already tells it the ground rules (it can't place bets, it has to ask
before anything risky, and so on). This message is just about you and how you like to work.

```
You're helping me with matched betting using my dashboard at [https://bets.example.com/bets/].

- I lay on [Smarkets, 0% commission].
- Anything else you should know: [e.g. I'm only doing sign up offers for now / I'm happy with 70% on free bets]
- I place every bet myself. When I send screenshots, work out the lay and check the bet first, and only
  record it once I tell you it's placed and the lay is fully matched.
- Only settle a bet when I tell you the result. Don't guess.
- If anything's unclear, ask me rather than assuming.

Start by telling me what needs doing today.
```

After that you can just talk to it normally, for example "here's my Sky Bet slip and the Smarkets lay",
"what free bets have I got left?" or "settle the Arsenal game, the draw came in".

## Which assistants work

I've tested it with ChatGPT, and the steps to connect it are in [docs/chatgpt-plugin.md](chatgpt-plugin.md).
The connection uses MCP, an open standard that other assistants support too, so they may well work, but I
haven't tried them yet.
