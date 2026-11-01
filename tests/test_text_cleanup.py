"""Which words of the store text count: only the ones that can tell games apart."""

from shelflife.learned import LARGE_LIBRARY, MIN_TEXT_WORDS, _dot, text_vectors

# A made-up library big enough for the rules that need many texts: each game describes itself with
# words from a shared pool, so every word is used by a handful of games.
POOL = [f"word{chr(97 + i)}{chr(97 + j)}" for i in range(26) for j in range(10)]


def library(n=LARGE_LIBRARY + 20):
    return {i: " ".join(POOL[(i * 7 + k) % len(POOL)] for k in range(30)) for i in range(n)}


def test_store_boilerplate_never_counts():
    texts = library()
    texts[1000] = texts[1] + " steam esc alt logo windows controller dlc"
    texts[1001] = texts[2] + " steam esc alt logo windows controller dlc"
    vectors = text_vectors(texts)
    assert not {"steam", "esc", "logo", "windows", "dlc"} & set(vectors[1000])
    assert _dot(vectors[1000], vectors[1001]) == _dot(vectors[1], vectors[2])  # the junk adds no likeness


def test_a_word_only_two_games_use_does_not_count_in_a_large_library():
    texts = library()
    texts[1], texts[2] = texts[1] + " zanzibar", texts[2] + " zanzibar"
    vectors = text_vectors(texts)
    assert "zanzibar" not in vectors[1]
    texts[3] = texts[3] + " zanzibar"  # a third game: now it can tell games apart
    assert "zanzibar" in text_vectors(texts)[1]


def test_a_word_most_games_use_does_not_count():
    texts = {i: t + " adventure" for i, t in library().items()}
    assert all("adventure" not in v for v in text_vectors(texts).values())


def test_a_text_with_too_few_telling_words_has_no_vector():
    texts = library()
    texts[2000] = "沙雕之路是一款 steam logo esc 游戏 " + POOL[0] + " " + POOL[1]  # what is left of a text in Chinese
    texts[2001] = " ".join(POOL[: MIN_TEXT_WORDS + 5])
    vectors = text_vectors(texts)
    assert vectors[2000] == {}  # compared on its tags alone
    assert vectors[2001]


def test_a_small_library_keeps_every_word_two_games_share():
    vectors = text_vectors({1: "stealth heist museum", 2: "stealth heist bank", 3: "farming cows"})
    assert "stealth" in vectors[1] and _dot(vectors[1], vectors[2]) > 0 and vectors[3] == {}
