cat > solution.py <<'PY'
def get_nth(lst, n):
    if 0 <= n < len(lst):
        return lst[n]
    return None
PY
