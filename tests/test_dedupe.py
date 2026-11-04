from signal_news.collector.dedupe import (
    ClusterCandidate,
    article_id,
    assign_cluster,
    canonicalize_url,
    normalise_title,
    same_story,
)


def test_canonicalize_strips_tracking_and_fragments():
    url = "http://www.Example.com/News/Story/?utm_source=rss&utm_medium=x&fbclid=abc&id=7#comments"
    assert canonicalize_url(url) == "https://example.com/News/Story?id=7"


def test_canonicalize_amp_variants():
    base = "https://example.com/news/story"
    assert canonicalize_url("https://example.com/news/story/amp/") == base
    assert canonicalize_url("https://example.com/amp/news/story") == base
    assert canonicalize_url("https://amp.example.com/news/story") == base
    assert (
        canonicalize_url("https://example.com/news/story.amp.html")
        == "https://example.com/news/story.html"
    )
    assert canonicalize_url("https://example.com/news/story?outputType=amp") == base


def test_canonicalize_sorts_query_and_keeps_meaningful_params():
    a = canonicalize_url("https://example.com/a?b=2&a=1")
    b = canonicalize_url("https://example.com/a?a=1&b=2&utm_campaign=z")
    assert a == b == "https://example.com/a?a=1&b=2"


def test_article_id_is_stable():
    assert article_id("https://example.com/a") == article_id("https://example.com/a")
    assert len(article_id("https://example.com/a")) == 8


def test_normalise_title_drops_outlet_suffix():
    assert normalise_title("Ivanti zero-day exploited - Reuters") == "ivanti zero day exploited"


def test_same_story_across_outlets():
    a = normalise_title("Ivanti warns of Connect Secure zero-day exploited in attacks")
    b = normalise_title(
        "Ivanti Connect Secure zero-day exploited in attacks, company warns - BleepingComputer"
    )
    assert same_story(a, b)


def test_different_stories_not_clustered():
    a = normalise_title("Apple releases iOS 26.1 with security fixes")
    b = normalise_title("Apple shares fall after earnings miss")
    assert not same_story(a, b)


def test_assign_cluster_returns_existing_cluster():
    cands = [
        ClusterCandidate(
            "a1",
            normalise_title("Fed holds interest rates steady, signals cuts next year"),
            "c-111111",
        ),
        ClusterCandidate("a2", normalise_title("Nvidia unveils new GPU"), "c-222222"),
    ]
    cid, matched = assign_cluster(
        normalise_title("Fed holds rates steady and signals cuts next year - CNBC"), cands
    )
    assert cid == "c-111111" and matched == "a1"
    assert assign_cluster(normalise_title("Totally unrelated headline about gardening"), cands) == (
        None,
        None,
    )


def test_assign_cluster_with_embeddings():
    cands = [ClusterCandidate("a1", "alpha beta", "c-1", vector=[1.0, 0.0, 0.0])]
    cid, _ = assign_cluster("gamma delta", cands, vector=[0.99, 0.05, 0.0], embedding_threshold=0.9)
    assert cid == "c-1"
