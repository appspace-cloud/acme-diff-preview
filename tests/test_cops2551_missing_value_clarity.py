"""The MISSING REQUIRED VALUE block must read as separate, ordered advice.

Live feedback on PR 3813: the block led with the chart's internal expression
($microservice.image.tag), and then crammed two unrelated pieces of advice
into a single long line ("define the missing value in customer.yaml or a
parent config.yaml" plus "if the chart version changed..."). An operator had
to untangle one wall of text to work out what they had actually done wrong.

The block must now read as distinct lines: what is wrong, where the chart
tripped, then each remedy on its own line. The nil-pointer shape also names
the concrete cause instead of only echoing the template variable.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("BB_USER", "t")
os.environ.setdefault("BB_TOKEN", "t")
os.environ.setdefault("ARGOCD_PASS", "t")
import diff_preview as m

# The exact stderr seen live on PR 3813.
NIL_PTR = ('template: appspace-micro-services/templates/configmaps/'
           'micro-versions-info.yaml:15:124: executing '
           '"appspace-micro-services/templates/configmaps/micro-versions-info.yaml" '
           'at <$microservice.image.tag>: nil pointer evaluating interface {}.tag')

REQUIRED = ('execution error at (appspace-micro-services/templates/configmaps/'
            'legacy-db-credentials.yaml:2:27): Missing required value: '
            '.Values.appspace.cloudShortName')


def test_nil_pointer_names_the_concrete_cause():
    out = m._explain_required_error(NIL_PTR)
    joined = "\n".join(out)
    # It must say which values block is at fault, not only the chart variable.
    assert "microservices.definitions" in joined
    assert "image" in joined
    # The chart location stays, as a separate line.
    assert any("micro-versions-info.yaml:15" in l for l in out)


def test_required_value_shape_still_names_the_values_path():
    out = m._explain_required_error(REQUIRED)
    joined = "\n".join(out)
    assert ".Values.appspace.cloudShortName" in joined
    assert any("legacy-db-credentials.yaml:2" in l for l in out)


def test_every_line_is_its_own_blockquote_line():
    """Each piece of advice must be a separate line so the renderer does not
    run them together into one paragraph."""
    for err in (NIL_PTR, REQUIRED):
        out = m._explain_required_error(err)
        assert len(out) >= 2, out
        for line in out:
            assert line.startswith("> "), line
            assert "\n" not in line, "a line must not embed newlines"


def test_remedies_are_separate_lines_not_one_blob():
    """Regression for the live complaint: the two remedies must not share a
    line. Build the block the way the comment builder does."""
    lines = m._explain_required_error(NIL_PTR) + m._missing_value_remedies()
    assert len(m._missing_value_remedies()) >= 2, "remedies must be split"
    blob = [l for l in lines if "customer.yaml" in l and "chart version" in l]
    assert not blob, f"two remedies crammed into one line: {blob}"


def test_remedies_stay_blockquote_lines():
    for line in m._missing_value_remedies():
        assert line.startswith("> "), line
        assert "\n" not in line
