# Reflection

**What assumptions did you make?**
The big ones are that timestamps are IST (the portal never says), that `kwh` is a running total, and that the transformer list is the source of truth for DT names. I also assumed this sits on a trusted network, so it has no auth of its own. The rest are in the README.

**Which part was the most difficult, and how did you get unstuck?**
Working out how to treat the hierarchy. I expected a tree with a few bad rows, so I started looking for which parent was "correct" for each code. Then I counted, and all 10 divisions sit under more than one circle, which isn't a few bad rows, that's just how the data is. So I stopped trying to fix it, built the tree from each meter's real path, and reported the overlaps. Finding the export was the other tricky bit. Guessing URLs got me nowhere, and it only made sense once I read the JS bundle behind the export button.

**If you had another day, what would you improve?**
A background sync of readings into SQLite. Right now the API answers questions about one meter well, but anything across many meters is stuck behind the rate limit, because there's no bulk path for readings. With that sync you could finally ask things like which transformers are running hot.

**What mistake did you make while solving this?**
I scanned energy data with 4 parallel workers without thinking about rate limits. 302 out of 403 requests got a 429, and the first summary I looked at was built on just 101 meters. It did teach me about the limit, but I should've started slow on someone else's system. I redid it one at a time and got no 429s. A smaller one: the login code first treated any failed session check as a wrong password, so a portal outage would've told you to check your credentials. A test caught that.

**If you were reviewing your own submission, what would you criticise?**
The tests use a fake portal I wrote from what I saw, so they can't catch the real one changing. `scripts/smoke.py` covers that, but only when someone runs it. Some numbers are also guesses: the 0.7s gap is just something that worked, not a known limit, and the cache TTLs are picked by feel. `main.py` has grown a bit long too, and I'd split the routes out if it grew further. Also, the daily consumption for once-a-day meters credits usage to the day the reading landed on, which is really the previous day's usage. That's simple and consistent, but someone could reasonably argue with it.
