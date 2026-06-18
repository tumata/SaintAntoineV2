import random

from saintantoine.selection import ShuffleBag, scan_tracks

TRACKS = [f"/music/{c}.mp3" for c in "abcde"]


def make_bag(tracks=TRACKS, seed=0):
    return ShuffleBag(tracks, rng=random.Random(seed))


def test_full_coverage_before_repeat():
    bag = make_bag()
    for _ in range(20):  # 4 full cycles
        cycle = {bag.next() for _ in range(len(TRACKS))}
        assert cycle == set(TRACKS)


def test_no_immediate_repeat_across_many_cycles():
    for seed in range(30):
        bag = make_bag(seed=seed)
        prev = None
        for _ in range(len(TRACKS) * 10):
            track = bag.next()
            assert track != prev
            prev = track


def test_cycle_boundary_no_repeat():
    # The last track of a cycle must never be the first of the next
    for seed in range(50):
        bag = make_bag(seed=seed)
        last_of_cycle = [bag.next() for _ in range(len(TRACKS))][-1]
        assert bag.next() != last_of_cycle


def test_exclude_current_track():
    bag = make_bag()
    current = bag.next()
    for _ in range(len(TRACKS) * 3):
        nxt = bag.next(exclude=current)
        assert nxt != current
        current = nxt


def test_single_track_replays():
    bag = make_bag(tracks=["/music/only.mp3"])
    assert bag.next() == "/music/only.mp3"
    assert bag.next(exclude="/music/only.mp3") == "/music/only.mp3"


def test_empty_pool_returns_none():
    bag = make_bag(tracks=[])
    assert bag.next() is None


def test_discard_removes_from_pool_and_bag():
    bag = make_bag()
    bag.next()  # start a cycle
    victim = TRACKS[2]
    bag.discard(victim)
    seen = {bag.next() for _ in range(len(TRACKS) * 4)}
    assert victim not in seen
    assert victim not in bag.tracks


def test_discard_everything_returns_none():
    bag = make_bag(tracks=["/music/a.mp3"])
    bag.discard("/music/a.mp3")
    assert bag.next() is None


def test_duplicates_collapsed():
    bag = ShuffleBag(["/m/a.mp3", "/m/a.mp3", "/m/b.mp3"], rng=random.Random(1))
    assert sorted(bag.tracks) == ["/m/a.mp3", "/m/b.mp3"]


def test_rescan_picks_up_new_tracks_on_refill():
    # Provider returns a growing library; new tracks appear at the next cycle.
    pool = list(TRACKS)
    bag = ShuffleBag(pool, rng=random.Random(0), track_provider=lambda: pool)
    first_cycle = {bag.next() for _ in range(len(TRACKS))}
    assert first_cycle == set(TRACKS)
    pool.append("/music/new.mp3")  # upload between cycles
    second_cycle = {bag.next() for _ in range(len(pool))}
    assert "/music/new.mp3" in second_cycle


def test_rescan_drops_deleted_tracks_on_refill():
    pool = list(TRACKS)
    bag = ShuffleBag(pool, rng=random.Random(0), track_provider=lambda: pool)
    [bag.next() for _ in range(len(TRACKS))]  # drain one cycle
    pool.remove(TRACKS[2])  # deleted between cycles
    seen = {bag.next() for _ in range(len(pool) * 3)}
    assert TRACKS[2] not in seen
    assert TRACKS[2] not in bag.tracks


def test_rescan_empty_keeps_working_pool():
    # A transient/empty scan must not wipe a working pool.
    results = [list(TRACKS), []]
    bag = ShuffleBag(TRACKS, rng=random.Random(0),
                     track_provider=lambda: results.pop(0) if results else [])
    [bag.next() for _ in range(len(TRACKS))]  # first refill consumes results[0]
    [bag.next() for _ in range(len(TRACKS))]  # second refill sees [] -> keep pool
    assert set(bag.tracks) == set(TRACKS)


def test_no_provider_never_rescans():
    bag = ShuffleBag(TRACKS, rng=random.Random(0))
    for _ in range(len(TRACKS) * 3):
        assert bag.next() in TRACKS
    assert set(bag.tracks) == set(TRACKS)


def test_scan_tracks(tmp_path):
    (tmp_path / "one.mp3").write_bytes(b"x")
    (tmp_path / "two.WAV").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "three.ogg").write_bytes(b"x")
    found = scan_tracks(tmp_path, [".mp3", ".wav", ".ogg", ".flac"])
    names = [f.rsplit("/", 1)[-1] for f in found]
    assert names == ["one.mp3", "three.ogg", "two.WAV"]


def test_scan_missing_folder(tmp_path):
    assert scan_tracks(tmp_path / "nope", [".mp3"]) == []
