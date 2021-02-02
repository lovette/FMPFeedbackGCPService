# FMPFeedbackGCPService

FMPFeedbackGCPService compliments the [FMPFeedbackForm](https://github.com/MacPaw/FMPFeedbackForm) project by providing [Google Cloud Platform](https://cloud.google.com/) hosted endpoints to store feedback submitted through an in-app feedback form within your macOS application. Endpoints are served using [Google Cloud Functions](https://cloud.google.com/functions) and data is stored in [Google Cloud Firestore](https://cloud.google.com/firestore).

Everything you need for a turnkey (and potentially free, if not low-cost) solution is included:

- Objective-C *FMPFeedbackGCPServiceSender* implementation
- Cloud Functions endpoints written in Python 3
- A handler to forward feedback via email through email service provider [Mailgun](https://www.mailgun.com/) using their REST API.

To get up and running, you need to setup and configure a few Google Cloud Platform services and integrate *FMPFeedbackForm* and *FMPFeedbackGCPServiceSender* into your macOS app.

## Services Architecture

The architecture involes a few services but is overall not too complicated.

![Services Architecture Diagram](https://user-images.githubusercontent.com/430169/106538830-e26e8f80-64ca-11eb-89c4-6023fcb2208d.png)

The basic workflow is:

1. macOS app (client) submits feedback to public Cloud Functions.
2. Cloud Functions store feedback in a Firestore collection.
3. A message is published to a Pub/Sub topic that notifies subscribers feedback was submitted.
4. Pub/Sub subscribers take action on the feedback, such as forwarding via email.
5. A periodic task performs housekeeping on the Firestore collection.

## Google Cloud Platform Deployment

[Google Cloud Platform](https://cloud.google.com/) provides a wide range of services with a boundless number of ways to get started. While the instructions below should give you a general idea what you need to accomplish, for the most part you're on your own in setting up an account and configuring your project.

Basic steps that you need to take include:

- Create a Service Account Identity
- Create a Pub/Sub Topic
- Create a few Cloud Functions
- Create a Cloud Scheduler Job

### Service Account Identity

Create a [Service Account](https://console.cloud.google.com/iam-admin/serviceaccounts) that the Cloud Functions and other services will assume as their identity. We use the name *fmpfeedback* below.


The service account will need these roles:

- Firebase Admin
- Pub/Sub Editor

Download the JSON keyfile for the service account and set the Runtime Environment Variable `GOOGLE_APPLICATION_CREDENTIALS` in file `.env` to its path. This will enable local development access to authentication credentials.

### Pub/Sub Topic

Create a [Pub/Sub Topic](https://console.cloud.google.com/cloudpubsub/topic/) that will receive a message when feedback is submitted. The default *Topic ID* is *fmpfeedback*. The topic name can be customized with the Runtime Environment Variable `FEEDBACK_FIRESTORE_COLLECTION` set in the Cloud Function properties or in a `.env` file saved alongside each Clound Function source.

### Cloud Functions

Create a [Cloud Function](https://console.cloud.google.com/functions/) for each of the functions in the `cloudfunctions` directory. The configuration properties for each function are detailed below.

Directory             | Function name                | Trigger | Executed function
--------------------- | ---------------------------- | ------- | -----------------
fmpfeedback_caretaker | fmpfeedback_caretaker        | HTTP    | fmpfeedback_caretaker
fmpfeedback_comment   | fmpfeedback_comment          | HTTP    | fmpfeedback_comment
fmpfeedback_mailgun   | fmpfeedback_mailgun_pubsub   | Pub/Sub | fmpfeedback_mailgun_pubsub
fmpfeedback_upload    | fmpfeedback_upload           | HTTP    | fmpfeedback_upload

The `cloudfunctions` directory contains the source code for each function. A quick way to get started is to copy and paste the code from `main.py` and `requirements.txt` into the Inline Editor.

You can  also deploy directly from this repository with [Cloud Source Repository](https://cloud.google.com/functions/docs/deploying/repo). Connect the repository to your project then choose the repository and set the *Directory with source code* to the corresponding `cloudfunctions` subdirectory. (Additional configuration will be required to redeploy Functions automatically when the underlying source code changes. See this [CI/CD tutorial](https://cloud.google.com/functions/docs/testing/test-cicd) for information on how to set this up.)

#### HTTP Cloud Functions Properties

Property                                 | Setting
---------------------------------------- | -------
Name                                     | *Entry point function name*
Trigger type                             | HTTP
Authentication                           | Allow unauthenticated invocations
Advanced > Service account               | *fmpfeedback*
Advanced > Runtime Environment Variables | See `.env` file alongside function source for details
Code > Runtime                           | Python 3.9
Code > Entry point                       | *Entry point function name*

All HTTP Cloud Functions should be created with the same *Region* selected and therefore be invoked using the same *Trigger URL* domain name.

#### Pub/Sub Cloud Function Properties

Property        | Setting
--------------- | -------
Name            | *Entry point function name*
Trigger type    | Cloud Pub/Sub
Topic           | `projects/YOUR-PROJECT-ID/topics/fmpfeedback`
Service account | *fmpfeedback*
Advanced > Runtime Environment Variables | See `.env` file alongside function source for details
Runtime         | Python 3.9
Entry point     | *Entry point function name*


### Cloud Scheduler Job

Create a [Cloud Scheduler](https://console.cloud.google.com/cloudscheduler/) job that will invoke the *fmpfeedback_caretaker* task on a regular schedule. This task performs routine housekeeping tasks on the feedback collection.

Property    | Setting
----------- | -------
Name        | fmpfeedback_caretaker
Description | Trigger fmpfeedback caretaker daily
Frequency   | 15 14 * * * (2 PM daily)
Timezone    | GMT
Target      | HTTP
URL         | *The https:// URL assigned to fmpfeedback_caretaker Cloud Function*
HTTP method | POST
Body        | `{}`

## App integration and deployment

### macOS app

You should have *FMPFeedbackForm* integrated and operational prior to setting up *FMPFeedbackGCPService*. Then all you need to do is switch your "sender" to *FMPFeedbackGCPServiceSender*.

### Sender domain

All HTTP Cloud Functions should be created with the same region selected and therefore be invoked using the same *Trigger URL* domain name. Pass this domain name as the `domain` parameter to the `initWithDomain` function of `FMPFeedbackGCPServiceSender`.

For example, with the HTTP trigger URL `https://REGION-PROJECT.cloudfunctions.net/ENTRY_POINT` you would pass `REGION-PROJECT.cloudfunctions.net` as the `domain` parameter.


### Sender authentication token

The macOS app and the Cloud Functions share a secret token that authenticates the app with the endpoints. This token can be any random sequence of characters and must be referenced in Functions `fmpfeedback_comment` and `fmpfeedback_upload`.

1. Generate a token by some means, such as `head -n 4096 /dev/urandom | openssl sha256`

2. Set the token as the value of Runtime Environment Variable `FEEDBACK_SENDER_AUTHTOKEN` either as a Cloud Function property or in a `.env` file uploaded alongside each Function source.

3. Pass the token as the `authToken` parameter to the `initWithDomain` function of `FMPFeedbackGCPServiceSender`.

### Mailgun ESP authentication and settings

The `fmpfeedback_mailgun` module provides a handler that forwards each feedback submission as an email message using the ESP [Mailgun](https://www.mailgun.com/) REST API. You need to have an  account with them if you want to use the module.

The `fmpfeedback_mailgun` module requires a few Runtime Environment Variables be set either as properties of the `fmpfeedback_mailgun_pubsub` Cloud Function or in a `.env` file uploaded alongside the Function source.

Variable           | Value
------------------ | -----
MAILGUN_API_KEY    | Mailgun API authentication token.
MAILGUN_API_DOMAIN | Mailgun API sending domain.
MAILGUN_SENDER     | Email address to send email feedback from.
MAILGUN_RECIPIENT  | Email address to send feedback to.

See the module `.env` file for more specific details.

## Local development

You need to have [Python 3](https://www.python.org/) and a web server such as [Caddy 2](https://caddyserver.com/) installed and setup to develop locally.

Create a virtualenv and install packages:

	make virtualenv
	source .venv/bin/activate
	make pip-sync

Start Python app in a terminal using *gunicorn*:

	dotenv run gunicorn main:app

Start *caddy server* in a second terminal to facilitate HTTPS endpoints:

	caddy run

You can then hit the endpoints:

	https://localhost/fmpfeedback_comment
	https://localhost/fmpfeedback_upload

The [Visual Studio Code](https://code.visualstudio.com/) workspace provided makes it easy to run and debug functions locally. Run the workspace Task *caddyserver* to facilitate HTTPS endpoints.
