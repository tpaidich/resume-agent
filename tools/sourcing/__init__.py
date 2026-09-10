"""Automated job sourcing: fetch postings from company ATS boards, filter,
score, and queue them for manual review.

Nothing in this package applies to a job. The pipeline ends at a scored row in
the review queue; submitting an application is always a human action.
"""
