"""
Mock "servicing console" — a deliberately LEGACY-styled target (brief §4).

Why local: we control the exceptional states (not-found, permission-denied,
session-timeout, surprise confirmation dialog) so we can DEMONSTRATE the error
taxonomy instead of describing it. Intentionally hostile: table layout, no test
IDs, non-semantic markup. A localhost app is still a real live surface, so the
discovery run requirement is fully satisfied.

Run:  flask --app apps/mock_bank/app run
TODO(build-together):
  * add /members/<id>/subaccounts/new with a confirmation dialog (irreversible)
  * add a query flag to inject: ?fail=timeout | permission | slow
  * seed a couple of members; make 00000 deliberately "not found"
"""
from flask import Flask, render_template, request

app = Flask(__name__)

MEMBERS = {
    "12345": {"name": "Jordan Rivers", "savings_balance": "$4,210.55"},
    # 00000 intentionally absent -> exercises MEMBER_NOT_FOUND
}


@app.get("/members/search")
def search():
    return render_template("search.html")


@app.get("/members/<mid>")
def detail(mid):
    member = MEMBERS.get(mid)
    if member is None:
        # legitimate BUSINESS OUTCOME, not an error page
        return render_template("not_found.html", mid=mid), 200
    return render_template("detail.html", m=member)


if __name__ == "__main__":
    app.run(debug=True)
