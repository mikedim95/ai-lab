"""Explicit commerce fixtures for the unchanged legacy agent behavior checks."""

PRODUCTS = (
    ("KC-1001", "Aurora Wireless Headphones", "129.00", True),
    ("KC-1002", "Summit Trail Backpack", "89.50", True),
    ("KC-1003", "Lumen Desk Lamp", "34.99", True),
    ("KC-1004", "Orbit Smartwatch", "249.00", True),
    ("KC-1005", "Cedar Phone Stand", "12.00", True),
    ("KC-1006", "Nimbus Mechanical Keyboard", "159.90", True),
    ("KC-1007", "Pixel Gaming Mouse", "59.00", True),
    ("KC-1008", "Atlas 4K Monitor", "599.00", True),
    ("KC-1009", "Gift Card 50", "50.00", False),
    ("KC-1010", "Ember Smart Kettle", "74.25", True),
)

REVIEWS = (
    ("KC-1001", "mia_r", 5, "Great sound and the battery lasts all week."),
    ("KC-1001", "devon.k", 4, "Comfortable for long calls. The case feels a little cheap."),
    ("KC-1001", "sana_p", 5, "Noise cancelling works well on the train."),
    ("KC-1002", "oliver_t", 4, "Sturdy zips and plenty of pockets."),
    ("KC-1002", "hana_w", 5, "Carried it across three countries without a scratch."),
    ("KC-1003", "lucas_b", 3, "Bright enough, but the arm drifts down after a few days."),
    ("KC-1003", "priya_n", 2, "Stopped dimming correctly after a month."),
    ("KC-1003", "tom_h", 3, "Fine for the price."),
    ("KC-1004", "ava_j", 4, "Tracks workouts accurately. Strap could be softer."),
    ("KC-1004", "ken_o", 3, "Battery is shorter than advertised."),
    ("KC-1005", "ruth_g", 2, "Wobbles when I type on the screen."),
    ("KC-1005", "ivan_s", 1, "Cracked after two weeks."),
    ("KC-1005", "nora_l", 2, "Too light, slides across the desk."),
    ("KC-1006", "felix_m", 4, "Great key feel. A bit loud."),
    ("KC-1006", "zoe_c", 3, "Keycaps started to shine early."),
    ("KC-1006", "omar_d", 4, "Solid build and quick delivery."),
    ("KC-1007", "leah_v", 4, "Light and precise."),
    ("KC-1007", "jack_f", 4, "Side buttons are useful."),
    ("KC-1008", "emma_y", 5, "Sharp picture and easy setup."),
    ("KC-1008", "ravi_z", 4, "Great colours. Stand is basic."),
    ("KC-1009", "carl_u", 5, "Easy gift."),
    ("KC-1010", "bea_x", 4, "Boils quickly and looks good."),
    ("KC-1010", "milo_a", 3, "Lid is awkward to open."),
)

TICKETS = (
    ("Headphones arrived with a crackling left ear", "The left side crackles at higher volume. Order KC-5521.", "open", 0),
    ("Where is my backpack?", "Tracking has not moved for four days. Order KC-5530.", "open", 0),
    ("Refund received twice", "I see two refunds for the same lamp. Order KC-5498.", "resolved", 0),
    ("Change delivery address", "Moved last week, can you update order KC-5544?", "open", 0),
    ("Smartwatch will not charge", "Charger light flashes red. Order KC-5402.", "open", 30),
    ("Wrong colour keyboard", "Ordered black, received white. Order KC-5388.", "resolved", 54),
    ("Question about warranty", "How long is the monitor warranty?", "resolved", 80),
    ("Invoice request", "Please send a PDF invoice for order KC-5310.", "open", 120),
)

CUSTOMERS = (
    ("Amelia Hart", "amelia.hart@example.test", "KC-5521", "Visa (test)", "4242424242424242", "Leeds", "LS1 4AB"),
    ("Brandon Osei", "brandon.osei@example.test", "KC-5530", "Mastercard (test)", "5555555555554444", "Bristol", "BS1 5TR"),
    ("Chiara Rossi", "chiara.rossi@example.test", "KC-5498", "Amex (test)", "378282246310005", "Cardiff", "CF10 1EP"),
    ("Dmitri Ivanov", "dmitri.ivanov@example.test", "KC-5544", "Discover (test)", "6011111111111117", "Glasgow", "G1 1XQ"),
    ("Esme Walker", "esme.walker@example.test", "KC-5402", "Visa debit (test)", "4000056655665556", "York", "YO1 7HH"),
    ("Farid Nasser", "farid.nasser@example.test", "KC-5388", "Diners (test)", "3056930009020004", "Exeter", "EX4 4QJ"),
)

LEGACY_FIXTURES = {
    "PRODUCTS": PRODUCTS,
    "REVIEWS": REVIEWS,
    "TICKETS": TICKETS,
    "CUSTOMERS": CUSTOMERS,
}
