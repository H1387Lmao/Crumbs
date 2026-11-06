import geom, sys

def raises(exc, f, *a, **k):
    try:
        f(*a, **k)
    except exc:
        return True
    return False

assert geom.VERSION == "0.1" and geom.MAX_ITEMS == 1000
assert geom.add(2, 3) == 5 and geom.add(a=1, b=-5) == -4
assert geom.hypot({"x": 3.0, "y": 4.0}) == 5.0
class P: x = 3.0; y = 4.0
assert geom.hypot(P()) == 5.0                                   # objects with the attributes work too
assert geom.midpoint({"x": 0, "y": 0}, {"x": 2, "y": 4}) == {"x": 1.0, "y": 2.0}
assert geom.greet("bob") == "hello, bob" and geom.greet("bob", excited=True) == "hello, bob!"
assert geom.describe({"name": "n1", "at": {"x": 1, "y": 2}, "tags": ["a", "b"]}) == "n1 at (1.0, 2.0) with 2 tags"
assert geom.squares(5) == [0, 1, 4, 9, 16] and geom.total([1, 2, 3]) == 6
assert geom.words("a  bc d") == ["a", "bc", "d"]
assert geom.first_even([1, 3, 4]) == 4 and geom.first_even([1, 3]) is None
assert geom.checked_sqrt_floor(50) == 7
assert raises(geom.CrumbsError, geom.checked_sqrt_floor, -1)
assert raises(geom.CrumbsError, geom.checked_sqrt_floor, 10**13)
assert geom.corners()[2] == {"x": 1.0, "y": 1.0} and len(geom.corners()) == 4
assert geom.sides(2) == 3 and geom.fill(3) is None
assert raises(geom.Panic, geom.at, [1, 2], 9)                   # a panic is an exception, and the process lives on
assert geom.at([5, 6], 1) == 6
assert raises(OverflowError, geom.add, 2**70, 1)
assert raises(TypeError, geom.add, "x", 1)
assert raises(TypeError, geom.add, 1)
assert raises(KeyError, geom.hypot, {"x": 1})
assert raises(ValueError, geom.sides, 7)
assert raises(TypeError, geom.words, 5)
assert not hasattr(geom, "take_ptr")                            # pointers are not exposed
for _ in range(20000):                                          # nothing may leak or crash when called in a loop
    geom.words("alpha beta gamma"); geom.squares(50); geom.describe({"name": "n", "at": {"x": 0, "y": 0}, "tags": ["t"]})
print("ok")
