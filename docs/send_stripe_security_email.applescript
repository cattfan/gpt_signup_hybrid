-- Open a Mail.app draft addressed to Stripe Security with the coordinated
-- disclosure body. Draft is shown to the user; nothing is sent automatically.
--
-- Usage:
--   osascript docs/send_stripe_security_email.applescript <path-to-body.txt>

on run argv
    if (count of argv) < 1 then
        error "Missing argument: path to body text file."
    end if

    set bodyPath to item 1 of argv
    set bodyFile to POSIX file bodyPath
    set bodyContent to read bodyFile as «class utf8»

    set targetEmail to "security@stripe.com"
    set emailSubject to "Possible abuse-protection gap on UPI PaymentIntent creation — coordinated disclosure"

    tell application "Mail"
        activate
        set newMessage to make new outgoing message with properties {subject:emailSubject, content:bodyContent, visible:true}
        tell newMessage
            make new to recipient at end of to recipients with properties {address:targetEmail}
        end tell
    end tell
end run
