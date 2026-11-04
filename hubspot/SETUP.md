# "How did we do?": HubSpot setup

When a deal reaches **Closed won**, HubSpot emails the client five stars.

- **5 stars:** the client sees a "Leave us a Google review" button.
- **1–4 stars:** the client sees a private feedback form. The rating and comment are saved on their HubSpot contact, and the deal owner gets a follow-up task.

```
Deal → Closed won ─▶ HubSpot workflow sends "How did we do?" email (5 star links)
                         │ client taps a star
                         ▼
              reviews.ciainsures.com/api/r  ── records rating, verifies contact ID + email
                         │                    └─ writes to HubSpot: rating property, note on contact + deal,
                         │                       and (below 5) a high-priority follow-up task for the deal owner
                         ▼
              /feedback/ page:  5★ → Google review button      1–4★ → private comment form → note in HubSpot
```

Requires Marketing Hub Professional or higher (deal-based workflows that send marketing email).

## 1. Point the reviews subdomain at the server (one DNS record)
At your DNS provider add an **A record**: `reviews` → `192.3.180.166`.
This doesn't touch the main website or email. Then, on the server:
```
certbot --nginx -d reviews.ciainsures.com --redirect -m contactus@ciainsures.com --agree-tos -n
```

## 2. Create a HubSpot private app (API access for the server)
HubSpot → Settings → Integrations → **Private Apps** → Create.
- Name: `CIA website reviews`
- Scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.deals.read`, `crm.schemas.contacts.write`
- Copy the access token and add it to the server in `/etc/ciainsures/api.env`:
  `HUBSPOT_TOKEN=pat-na1-...`, then run `systemctl restart ciainsures-api`.

On restart the server creates these contact properties automatically:
`CIA review rating`, `CIA review comment`, `CIA review date`, `CIA review: clicked Google`.

## 3. Add the email template
Marketing → Files and Templates → **Design Tools** → New file → *Coded file* → **Email template**.
Paste the contents of `how-did-we-do-email.html` and publish.
Then Marketing → Email → Create email → *Automated* → choose the **CIA - How did we do (post-signing)** template.
- Subject: `How did we do, {{ contact.firstname }}?`
- Subscription type: create one called **Service & feedback**
  (Settings → Marketing → Email → Subscription types) so clients who opt out of marketing still get service emails.

## 4. Create the workflow
Automation → Workflows → Create → **Deal-based**, from scratch.
1. **Enrollment trigger:** Deal stage is any of *Closed won* (for every pipeline you use).
   Turn on **re-enrollment** for that trigger, so repeat clients are asked after each new deal.
2. **Delay:** 3 days (time for the policy documents to arrive).
3. **Send email:** the "How did we do?" email, *to associated contacts*.
4. *(Optional)* **Branch** on contact property `CIA review rating` after 7 days: if unknown, send one reminder.

Optional extra workflow (contact-based): when `CIA review rating` is less than 5, send an internal email notification to a manager.
The server already creates a follow-up task for the deal owner.

## 5. Settings on the server (`/etc/ciainsures/api.env`)
| Setting | Meaning |
|---|---|
| `HUBSPOT_TOKEN` | Private-app token from step 2 |
| `REVIEW_THRESHOLD=5` | Rating that shows the Google review button |
| `SMTP_*`, `NOTIFY_TO` | Optional extra email alerts from the server |

And in `config.json` in this project: set `google_review_url` to your Google review link
(Google Business Profile → **Ask for reviews** → copy link), then publish.

## Test it
Create a test contact with your own email, a test deal associated to it, and move the deal to Closed won
(set the workflow delay to 0 while testing). Tap 3 stars: you should see the private form, a note and a task in HubSpot.
Then tap 5 stars: the Google button appears and `CIA review rating` changes to 5.
