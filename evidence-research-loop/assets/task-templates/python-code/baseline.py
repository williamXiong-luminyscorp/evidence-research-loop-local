def solve(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    result.sort()
    return result
