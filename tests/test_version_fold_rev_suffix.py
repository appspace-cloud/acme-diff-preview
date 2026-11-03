"""The version fold must recognise a bump away from (or to) a -revN tag.

An environment on a -revN hotfix tag moving to the next release carries the
base release in `appspace_platform_version`, without the suffix. That env
value never matched the chart transition, so apigateway and
apigateway-webhook were listed as "changed for another reason", while an
environment moving from the plain base tag folded the same lines.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from version_fold import _classify_version_fold  # noqa: E402


def _image_section(i):
    return (f"/apps/Deployment svc-{i}",
            "--- \n+++ \n@@ -55,7 +55,7 @@\n"
            "       containers:\n"
            f"-        image: docker-dev.repo.appspace.com/svc-{i}:1.116.12\n"
            f"+        image: docker-dev.repo.appspace.com/svc-{i}:1.116.13\n")


def _apigateway(old_env, new_env):
    return ("/apps/Deployment apigateway",
            "--- \n+++ \n@@ -35,7 +35,7 @@\n"
            "       annotations:\n"
            "-        checksum/micro-versions-info.yaml: b756c89b930d4e1a6a9580be2becbf19be0b53e0d8318ce4b07dc2f3533d0c52\n"
            "+        checksum/micro-versions-info.yaml: 692575ea20a7c7df03d589ac4d03c8f332280ed032424191179f7d17b45fda4e\n"
            "@@ -177,11 +177,11 @@\n"
            "         - name: appspace_platform_releaseDate\n"
            "-          value: \"2026-10-21T20:00:00Z\"\n"
            "+          value: \"2026-11-03T20:00:00Z\"\n"
            "         - name: appspace_platform_version\n"
            f"-          value: {old_env}\n"
            f"+          value: {new_env}\n")


def _sections(old_env, new_env):
    return [_image_section(i) for i in range(3)] + [_apigateway(old_env,
                                                                new_env)]


def test_bump_from_a_rev_tag_folds_the_base_version_env_value():
    fold = _classify_version_fold(_sections("2603.2.28", "2603.2.29"),
                                  ("2603.2.28-rev3", "2603.2.29"))
    assert "/apps/Deployment apigateway" in fold["headers"]


def test_bump_to_a_rev_tag_folds_the_base_version_env_value():
    fold = _classify_version_fold(_sections("2603.2.28", "2603.2.29"),
                                  ("2603.2.28", "2603.2.29-rev1"))
    assert "/apps/Deployment apigateway" in fold["headers"]


def test_bare_bump_still_folds():
    fold = _classify_version_fold(_sections("2603.2.28", "2603.2.29"),
                                  ("2603.2.28", "2603.2.29"))
    assert "/apps/Deployment apigateway" in fold["headers"]


def test_an_unrelated_value_change_still_stays_inline():
    fold = _classify_version_fold(_sections("2603.2.27", "2603.2.29"),
                                  ("2603.2.28-rev3", "2603.2.29"))
    assert "/apps/Deployment apigateway" not in fold["headers"]
