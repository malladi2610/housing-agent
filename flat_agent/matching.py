from .models import ListingInput, MatchResult, Profile


def evaluate(listing: ListingInput, profile: Profile) -> MatchResult:
    failures, unknowns = [], []
    total = listing.monthly_total()
    if total is not None and total > profile.max_monthly_total:
        failures.append(f"€{total:.2f} total exceeds the €{profile.max_monthly_total:.0f} ceiling")
    elif listing.warm_rent is not None and listing.warm_rent > profile.max_monthly_total:
        failures.append("Warm rent alone exceeds the total monthly budget")
    if total is None or not listing.all_costs_confirmed:
        unknowns.append("Confirm the full monthly cost, including electricity, internet and other mandatory charges")
    if listing.home_type == "unknown":
        unknowns.append("Accommodation type is unknown")
    elif listing.home_type not in profile.home_types:
        failures.append("Accommodation type is outside the search profile")
    if listing.available_from is None:
        unknowns.append("Move-in date is unknown")
    elif listing.available_from > profile.latest_move_in:
        failures.append("Available after the latest move-in date")
    elif not listing.move_in_confirmed:
        unknowns.append("Confirm an acceptable tenancy start date and any earlier rent liability")
    if listing.available_until and listing.available_until < profile.target_move_in:
        failures.append("The tenancy ends before the target move-in")
    if listing.distance_km is None:
        unknowns.append("Distance to MTZ needs verification")
    elif listing.distance_km > profile.max_distance_km:
        failures.append(f"Distance exceeds {profile.max_distance_km:g} km")
    elif listing.distance_basis != "route":
        unknowns.append("Verify route distance; a straight-line radius is only an initial screen")
    if failures:
        status, reasons = "excluded", failures + unknowns
    elif unknowns:
        status, reasons = "needs_review", unknowns
    else:
        status, reasons = "match", ["Meets the confirmed cost, housing, move-in and distance requirements"]
    return MatchResult(status=status, reasons=reasons, monthly_total=total, profile_revision=profile.revision)
