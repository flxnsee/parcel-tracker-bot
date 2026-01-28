# Parcel Tracking Telegram Bot 📦🤖

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Framework-Flask-000000?logo=flask&logoColor=white)
![MongoDB](https://img.shields.io/badge/Database-MongoDB-green?logo=mongodb&logoColor=white)
![Telegram](https://img.shields.io/badge/Platform-Telegram-blue?logo=telegram)
![Status](https://img.shields.io/badge/Status-Beta-yellow)

The **Parcel Tracking Telegram Bot** is an automated tracking assistant that helps users monitor the status of their parcels directly in Telegram. It integrates with the **ParcelsApp API** to fetch real-time shipment updates and automatically notifies users whenever the delivery status changes.

The bot supports multiple tracked parcels per user, keeps delivery history, and presents updates in a clean, emoji-enhanced format for better readability.

⚠️ **This project is still under active development.** Some edge cases, API limitations, and performance optimizations are still being worked on.

---

## Features 🚀

- **Live Parcel Tracking**  
  Track parcels worldwide using a single tracking number.

- **Automatic Status Updates**  
  Users receive Telegram notifications whenever the shipment status changes.

- **Multiple Parcel Support**  
  Track several parcels simultaneously per user.

- **Detailed Shipment Info**  
  View full shipment history, route, and last update time.

- **Emoji-Based UI**  
  Friendly and readable message formatting with country flags and icons.

- **Persistent Storage**  
  MongoDB is used to store users, subscriptions, and tracking history.

---

## Bot Commands 🤖

| Command | Description |
|------|-----------|
| `/start` | Show welcome message and available commands |
| `/track <NUMBER>` | Start tracking a parcel |
| `/list` | Show all tracked parcels |
| `/untrack <NUMBER>` | Stop tracking a parcel |
| `/info <NUMBER>` | Show detailed parcel information and history |

---

## Getting Started 🛠️

### Prerequisites

- **Python 3.8+**
- **MongoDB** (local or cloud)
- **Telegram Bot Token**
- **ParcelsApp API Key**

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/flxnsee/parcel-tracking-bot.git
   cd parcel-tracking-bot
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set environment variables**:
   ```bash
   export TELEGRAM_TOKEN=your_telegram_bot_token
   export PARCELS_API_KEY=your_parcels_api_key
   export MONGO_URL=your_mongodb_connection_string
   ```

4. **Run the bot**:
   ```bash
   python tracker.py
   ```

5. **Set Telegram Webhook** (example):
   ```bash
   https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=<YOUR_SERVER_URL>/telegram-webhook
   ```

---

## Project Structure 📂

```plaintext
parcel-tracking-bot/
├── tracker.py            # Main Flask app & Telegram bot logic
├── requirements.txt      # Python dependencies
└── README.md             # Project documentation
```

---

## Tech Stack ⚙️

- **Python** – core language
- **Flask** – webhook server
- **MongoDB** – data storage
- **Telegram Bot API** – user interaction
- **ParcelsApp API** – shipment tracking data

---

## Known Limitations ⚠️

- Dependent on third-party API response time
- Some carriers may return incomplete tracking history
- Webhook server must be publicly accessible

---

## Future Plans 🚀

- Add inline buttons (Telegram keyboards)
- Improve caching to reduce API calls
- Add carrier detection and filtering
- Dockerize the project
- Add multi-language support

---

## Contributing 🤝

Contributions are welcome!

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/new-feature`)
3. Commit your changes (`git commit -m 'Add new feature'`)
4. Push to the branch (`git push origin feature/new-feature`)
5. Open a Pull Request

---

## License 📄

This project is licensed under the MIT License.

---

Made with ❤️ and Python

