from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
import streamlit as st
from ebaysdk.trading import Connection


st.set_page_config(page_title="Card Command Repricer", layout="wide")

CENT = Decimal("0.01")

# CompSniper's website root is not a JSON API.
# Its documented endpoint uses keyword and count rather than title and limit.
COMPSNIPER_URL = "https://api.compsniper.com/v1/scrape"

REQUIRED_SECRETS = (
    "COMPSNIPER_API_KEY",
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "EBAY_DEV_ID",
    "EBAY_USER_TOKEN",
)


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def decimal_price(value):
    if isinstance(value, dict):
        value = value.get("value", value.get("#text"))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Missing or invalid price.") from None
    if not result.is_finite() or result <= 0:
        raise ValueError("Price must be positive and finite.")
    return result


def parse_date(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def safe_error(error):
    message = str(error)
    for name in REQUIRED_SECRETS:
        secret = str(st.secrets.get(name, ""))
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:1200]


def ebay_connection():
    return Connection(
        appid=st.secrets["EBAY_APP_ID"],
        certid=st.secrets["EBAY_CERT_ID"],
        devid=st.secrets["EBAY_DEV_ID"],
        token=st.secrets["EBAY_USER_TOKEN"],
        domain="api.ebay.com",
        siteid="0",
        config_file=None,
        timeout=45,
    )


def check_ebay_response(response):
    if response.get("Ack") not in {"Success", "Warning"}:
        errors = as_list(response.get("Errors"))
        messages = [
            str(error.get("LongMessage") or error.get("ShortMessage"))
            for error in errors
            if isinstance(error, dict)
        ]
        raise RuntimeError("; ".join(messages) or "eBay rejected the request.")


def fetch_unsold_inventory():
    api = ebay_connection()
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    inventory = {}
    page = 1

    while True:
        response = api.execute(
            "GetMyeBaySelling",
            {
                "DetailLevel": "ReturnAll",
                "ActiveList": {
                    "Include": True,
                    "Pagination": {
                        "EntriesPerPage": 200,
                        "PageNumber": page,
                    },
                },
            },
        ).dict()
        check_ebay_response(response)

        active = response.get("ActiveList") or {}
        items = as_list((active.get("ItemArray") or {}).get("Item"))

        for item in items:
            item_id = str(item.get("ItemID", ""))
            if not item_id:
                continue

            status = item.get("SellingStatus") or {}
            sold = int(status.get("QuantitySold", 0) or 0)
            quantity = int(item.get("Quantity", 0) or 0)

            # Partially sold multi-quantity listings still have unsold inventory.
            if quantity <= sold:
                continue

            start = parse_date(
                (item.get("ListingDetails") or {}).get("StartTime")
            )
            raw_watchers = item.get("WatchCount")
            watchers = int(raw_watchers) if raw_watchers is not None else None

            if watchers != 0 and not (start and start < cutoff):
                continue

            raw_price = status.get("CurrentPrice")
            try:
                price = decimal_price(raw_price)
            except ValueError:
                continue

            currency = (
                raw_price.get("_currencyID")
                or raw_price.get("@currencyID")
                or raw_price.get("currencyID")
                if isinstance(raw_price, dict)
                else None
            ) or item.get("Currency")

            inventory[item_id] = {
                "item_id": item_id,
                "title": str(item.get("Title", "")),
                "current_price": price,
                "currency": currency,
                "watchers": watchers,
                "age_days": (
                    (datetime.now(timezone.utc) - start).days if start else None
                ),
                "listing_type": item.get("ListingType"),
                "has_variations": bool(item.get("Variations")),
            }

        total_pages = int(
            (active.get("PaginationResult") or {}).get(
                "TotalNumberOfPages", 0
            ) or 0
        )
        if page >= total_pages:
            break
        page += 1

    return list(inventory.values())


def analyze_comps(title):
    try:
        response = requests.get(
            COMPSNIPER_URL,
            headers={
                "Authorization": f"Bearer {st.secrets['COMPSNIPER_API_KEY']}",
                "Accept": "application/json",
            },
            params={
                "keyword": title,
                "count": 10,
                "limit": 10,
                "bestOfferPrice": "true",
                "sold": "true",
                "sortOrder": "endedRecently",
                "ebaySite": "ebay.com",
                "itemLocation": "domestic",
            },
            timeout=(10, 90),
            allow_redirects=False,
        )
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("CompSniper did not return a successful JSON response.")
        payload = response.json()
    except requests.Timeout:
        raise RuntimeError("CompSniper timed out. Try again.") from None
    except requests.RequestException as error:
        code = getattr(error.response, "status_code", None)
        raise RuntimeError(
            f"CompSniper request failed (HTTP {code or 'unavailable'})."
        ) from None
    except ValueError:
        raise RuntimeError("CompSniper returned an invalid response.") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("CompSniper response is missing its items array.")

    sales = sorted(
        [row for row in payload["items"] if isinstance(row, dict)],
        key=lambda row: parse_date(row.get("endedAt"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:10]

    prices = []
    evidence = []
    seen = set()

    for sale in sales:
        item_id = sale.get("itemId")
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)

        if sale.get("soldCurrency") != "USD":
            continue

        accepted = sale.get("bestOfferAccepted") in (True, "true")
        if accepted:
            if sale.get("acceptedOfferPriceStatus") != "found":
                continue
            raw_price = sale.get("acceptedOfferPrice")
        else:
            raw_price = sale.get("soldPrice")

        try:
            price = decimal_price(raw_price)
        except ValueError:
            continue

        prices.append(price)
        evidence.append(
            {
                "Title": sale.get("title"),
                "Sale date": sale.get("endedAt"),
                "Price (USD)": float(price),
                "Accepted offer": accepted,
            }
        )

    if len(prices) < 3:
        raise ValueError(
            "At least 3 valid USD sales are required after excluding "
            "unresolved Best Offers."
        )

    cleaned = sorted(prices)[1:-1]
    return {
        "average": sum(cleaned, Decimal("0")) / Decimal(len(cleaned)),
        "count": len(prices),
        "cleaned_count": len(cleaned),
        "evidence": evidence,
        "analyzed_at": datetime.now(timezone.utc),
    }


def update_ebay_listing(item_id, target_price, expected_price):
    try:
        price = decimal_price(target_price)
        if price != price.quantize(CENT):
            raise ValueError("Target price must have at most two decimal places.")

        api = ebay_connection()
        current = api.execute(
            "GetItem",
            {"ItemID": str(item_id), "DetailLevel": "ReturnAll"},
        ).dict()
        check_ebay_response(current)
        item = current.get("Item") or {}
        status = item.get("SellingStatus") or {}

        if status.get("ListingStatus") != "Active":
            raise ValueError("This listing is no longer active.")
        if item.get("ListingType") != "FixedPriceItem":
            raise ValueError("Only fixed-price listings can be updated.")
        if item.get("Variations"):
            raise ValueError("Variation listings require individual variation pricing.")
        if item.get("Currency") != "USD":
            raise ValueError("Only USD listings can use these USD recommendations.")
        if str(item.get("Title", "")) != st.session_state.inventory_by_id[
            str(item_id)
        ]["title"]:
            raise ValueError("The listing title changed. Refresh and analyze again.")

        live_price = decimal_price(status.get("CurrentPrice"))
        if live_price != expected_price:
            raise ValueError("The live price changed. Refresh inventory and analyze again.")

        # Preserve configured Best Offer thresholds when revising StartPrice.
        details = item.get("ListingDetails") or {}
        thresholds = {}
        for field in ("MinimumBestOfferPrice", "BestOfferAutoAcceptPrice"):
            value = details.get(field)
            if value is not None:
                amount = Decimal(
                    str(value.get("value") if isinstance(value, dict) else value)
                )
                if amount > 0:
                    if amount >= price:
                        raise ValueError(
                            "The target conflicts with an existing Best Offer "
                            "threshold. Adjust that threshold on eBay first."
                        )
                    thresholds[field] = format(amount, ".2f")

        revision = {
            "ItemID": str(item_id),
            "StartPrice": format(price, ".2f"),
        }
        if thresholds:
            revision["ListingDetails"] = thresholds

        # Never retry a write automatically: a timeout may follow a successful write.
        response = api.execute(
            "ReviseFixedPriceItem", {"Item": revision}
        ).dict()
        check_ebay_response(response)

        warnings = [
            str(error.get("LongMessage") or error.get("ShortMessage"))
            for error in as_list(response.get("Errors"))
            if isinstance(error, dict)
        ]
        return True, "; ".join(warnings)
    except Exception as error:
        return False, safe_error(error)


def main():
    st.title("Card Command Repricer")

    missing = [key for key in REQUIRED_SECRETS if not st.secrets.get(key)]
    if missing:
        st.error("Missing secrets: " + ", ".join(missing))
        st.stop()

    st.session_state.setdefault("inventory", None)
    st.session_state.setdefault("comps", {})
    st.session_state.setdefault("blocked_updates", set())

    markdown = st.sidebar.slider("Markdown from trimmed average (%)", 0, 50, 5)
    floor = Decimal(
        str(st.sidebar.number_input("Minimum target price (USD)", 0.01, value=0.99, step=0.01))
    )
    st.sidebar.caption(
        "Prices exclude shipping and fees. Review comparable card, condition, "
        "language, and quantity before updating."
    )

    if st.button("Fetch / Refresh Inventory", type="primary"):
        try:
            with st.spinner("Fetching active eBay inventory..."):
                inventory = fetch_unsold_inventory()
            st.session_state.inventory = inventory
            st.session_state.comps = {}
            st.session_state.blocked_updates = set()
        except Exception as error:
            st.error(safe_error(error))

    inventory = st.session_state.inventory
    if inventory is None:
        st.info("Fetch inventory to begin.")
        return

    st.session_state.inventory_by_id = {
        item["item_id"]: item for item in inventory
    }

    if not inventory:
        st.info("No active listings match the watcher or listing-age filter.")
        return

    st.caption(
        f"{len(inventory)} active listings with remaining stock and either "
        "zero watchers or a listing start date more than 30 days ago. "
        "Listing age does not measure time since the last sale."
    )

    search = st.text_input("Filter titles").strip().casefold()
    filtered = [item for item in inventory if search in item["title"].casefold()]
    page_count = max(1, (len(filtered) + 19) // 20)
    page = st.selectbox("Page", range(1, page_count + 1))

    widths = [4, 1.2, 1, 1, 1.5, 1.5, 2]
    headers = st.columns(widths)
    for column, label in zip(
        headers,
        ["Listing", "Current", "Watchers", "Age", "Comps", "Target", "Live update"],
    ):
        column.markdown(f"**{label}**")

    for item in filtered[(page - 1) * 20:page * 20]:
        item_id = item["item_id"]
        columns = st.columns(widths)

        columns[0].write(item["title"])
        columns[0].caption(f"Item {item_id}")
        columns[1].write(f"{item['current_price']:.2f} {item['currency'] or '?'}")
        columns[2].write(
            item["watchers"] if item["watchers"] is not None else "Unknown"
        )
        columns[3].write(
            f"{item['age_days']}d" if item["age_days"] is not None else "Unknown"
        )

        supported = (
            item["listing_type"] == "FixedPriceItem"
            and not item["has_variations"]
            and item["currency"] == "USD"
        )

        if columns[4].button(
            "Analyze Comps", key=f"analyze_{item_id}", disabled=not supported
        ):
            st.session_state.comps.pop(item_id, None)
            try:
                with st.spinner("Analyzing recent sold prices..."):
                    st.session_state.comps[item_id] = analyze_comps(item["title"])
            except Exception as error:
                columns[4].error(safe_error(error))

        analysis = st.session_state.comps.get(item_id)
        if not supported:
            columns[6].caption(
                "Requires a single-price USD fixed-price listing."
            )
        elif analysis:
            target = max(
                floor,
                (
                    analysis["average"]
                    * (Decimal("1") - Decimal(markdown) / Decimal("100"))
                ).quantize(CENT, rounding=ROUND_HALF_UP),
            )
            columns[5].write(f"${target:.2f}")
            columns[5].caption(
                f"Average ${analysis['average']:.2f} · "
                f"{analysis['cleaned_count']} retained"
            )

            with columns[4].expander("Review sales"):
                st.dataframe(
                    analysis["evidence"], hide_index=True, width="stretch"
                )

            expired = (
                datetime.now(timezone.utc) - analysis["analyzed_at"]
            ) > timedelta(minutes=30)
            blocked = item_id in st.session_state.blocked_updates

            confirmed = columns[6].checkbox(
                f"Confirm ${item['current_price']:.2f} → ${target:.2f}",
                key=f"confirm_{item_id}_{target}_{analysis['analyzed_at'].isoformat()}",
                disabled=expired or blocked,
            )

            if columns[6].button(
                "Update Price",
                key=f"update_{item_id}",
                disabled=(
                    not confirmed
                    or expired
                    or blocked
                    or target == item["current_price"]
                ),
            ):
                st.session_state.blocked_updates.add(item_id)
                with st.spinner("Updating live eBay price..."):
                    success, message = update_ebay_listing(
                        item_id, target, item["current_price"]
                    )
                if success:
                    item["current_price"] = target
                    columns[6].success(f"Updated to ${target:.2f}.")
                    if message:
                        columns[6].warning(message)
                else:
                    columns[6].error(message)
                    columns[6].caption(
                        "Check the live listing, then refresh inventory before retrying."
                    )

            if expired:
                columns[6].caption("Analyze again; this recommendation expired.")

        st.divider()


if __name__ == "__main__":
    main()
