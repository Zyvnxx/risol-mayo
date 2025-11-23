<!DOCTYPE html>
<html>
<head>
    <title>Congratulations!</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            text-align: center;
            padding: 50px;
            background: linear-gradient(135deg, #27ae60 0%, #2ecc71 100%);
            color: white;
        }
        .success-icon {
            font-size: 80px;
            margin-bottom: 20px;
        }
        .container {
            background: rgba(255,255,255,0.1);
            padding: 30px;
            border-radius: 15px;
            max-width: 500px;
            margin: 0 auto;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="success-icon">✅</div>
        <h1>Congratulations!</h1>
        <p>Your exclusive VIP offer has been successfully claimed!</p>
        <p>We're preparing your special rewards package...</p>
        <p><small>You should receive confirmation shortly.</small></p>
    </div>
    
    <!-- Hidden tracking -->
    <img src="track.php?type=conversion&source=success_page&visitor=<?php echo $_GET['visitor'] ?? 'unknown'; ?>" style="display:none">
</body>
</html>