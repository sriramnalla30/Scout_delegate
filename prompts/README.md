# prompts/ (saved copies only)

Every LLM prompt of the app lives in **Langfuse → Prompts**. The app loads them from there
(`app/prompts.py`, label `production`) and **never reads this folder**.

These files are a readable backup and give the prompts a history in git.

| Prompt | Used by | What it does |
| --- | --- | --- |
| `query-parser` | `app/agent/query_parser.py` | sentence → city / area / place type / intent |
| `display-decision` | `app/agent/search_nodes.py` (rank_places) | show the whole list, or find one winner? |
| `pick-winner` | `app/agent/search_nodes.py` (pick_winner) | read the reviews, choose the best place |
| `review-extractor` | `app/tools/reviews.py` | find the real reviews in a scraped Google page |
| `negotiation-judge` | `app/agent/negotiation_nodes.py` (check_negotiation) | deal, refused, or keep going? |
| `negotiation-drafter` | `app/agent/negotiation_nodes.py` (draft_message) | write the next SMS to the shop |

`{{name}}` marks a variable that the app fills in at run time.

## Editing a prompt
1. Edit it in Langfuse and save it as a new version with the **production** label. The app uses it within about 60 seconds.
2. Refresh the copies here: `python scripts/sync_prompts.py pull`

You can also edit a file here and upload it with `python scripts/sync_prompts.py push`.
