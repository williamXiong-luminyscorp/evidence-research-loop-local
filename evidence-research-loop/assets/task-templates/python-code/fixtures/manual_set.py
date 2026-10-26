def solve(values):
    unique = set()
    for value in values:
        unique.add(value)
    result = list(unique)
    result.sort()
    return result
