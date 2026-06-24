import pytest
from apply_engine.ats_detect import detect_ats, AtsKind


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/oura/jobs/4079033009", AtsKind.GREENHOUSE),
    ("https://boards.greenhouse.io/acme/jobs/123", AtsKind.GREENHOUSE),
    ("https://jobs.ashbyhq.com/acme/abc", AtsKind.ASHBY),
    ("https://illumina.wd1.myworkdayjobs.com/x/job/y", AtsKind.WORKDAY),
    ("https://jobs.lever.co/acme/abc", AtsKind.LEVER),
    ("https://www.linkedin.com/jobs/view/12345", AtsKind.LINKEDIN),
    ("https://careers.dexcom.com/job/123", AtsKind.UNKNOWN),
    ("", AtsKind.UNKNOWN),
])
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected
