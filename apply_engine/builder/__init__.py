"""apply_engine.builder — profile-driven resume + cover-letter generator.

Takes your structured profile (facts) + a target job description and drafts a tailored,
one-page resume and a four-beat cover letter via `claude -p`, then renders them to PDF.
The drafter grounds every claim in your supplied facts; it never invents.
"""
