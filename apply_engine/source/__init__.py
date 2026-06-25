"""source — STAGE 1 of the pipeline: scan public ATS feeds for new postings.

  feeds — scan Greenhouse/Lever/Ashby boards for each company in your watchlist
          for new, keyword-matched postings, dedupe against jobs.json, and write a
          JSON review queue. NEVER writes jobs.json — sourcing is read-only w.r.t.
          pipeline state; review the queue and hand stubs to the qualify stage.
"""
