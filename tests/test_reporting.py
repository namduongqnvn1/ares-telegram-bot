from reporting import parse_report_payload


def test_parse_report_payload_prefers_cash_remaining():
    report = parse_report_payload({
        "report_date": "05/07/2026",
        "fnet_shifts": [1366000, 2030000, 855000],
        "fnet_total": 4251000,
        "ffood_shifts": [490000, 766000, 281000],
        "ffood_total": 1537000,
        "transfer_shifts": [329000, 759000, 463000],
        "transfer_total": 1551000,
        "cash_total": 4432000,
        "cash_remaining": 3518000,
        "confidence": 0.95,
        "warnings": [],
    })

    assert report.report_date == "05/07/2026"
    assert report.machine_revenue == 4251000
    assert report.service_revenue == 1537000
    assert report.cash == 3518000
    assert report.momo == 1551000


def test_shift_total_mismatch_is_ignored_because_sheet_uses_total_cells():
    report = parse_report_payload({
        "report_date": "06/07/2026",
        "fnet_shifts": [965000, 2537000, 213000],
        "fnet_total": 3695000,
        "ffood_shifts": [673000, 599000, 312000],
        "ffood_total": 1584000,
        "transfer_shifts": [667000, 460000, 290000],
        "transfer_total": 1417000,
        "cash_total": 3862000,
        "cash_remaining": 3520000,
        "confidence": 0.9,
        "warnings": [],
    })

    assert report.machine_revenue == 3695000
    assert report.cash == 3520000
    assert not report.warnings
