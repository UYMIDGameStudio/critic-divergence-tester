# First revision: a five-minute guide

1. Run `py -3 critic_runner.py app` on Windows or `python3 critic_runner.py app` on macOS/Linux. The application opens only on this computer.
2. Choose a UTF-8 Markdown or TXT manuscript. It becomes immutable V1; the original file is never overwritten.
3. Paste an existing AI review report. Copy the generated atomization prompt to any AI, then paste its complete return into the application.
4. Check every finding. Accept only problems you want to address; reject or defer the rest. `UNVERIFIED` means the report's assertion has not become a fact.
5. Generate the constrained revision prompt and run it with any AI. If the response is invalid, copy the repair prompt; the failed response remains available.
6. Review each diff hunk. Accept, reject, edit before accepting, or request regeneration. Every applied hunk must name its Finding and RevisionAction.
7. Generate V2. A changed base hash, ambiguous quote, out-of-scope ID, or overlapping hunk stops the operation instead of guessing.
8. Recheck V2 using the original finding criteria, confirm each proposed status yourself, and export.

The export folder contains the V2 manuscript, a revision checklist, and complete audit files. Keep the whole project folder if you want to reconstruct or reverse a decision later.

