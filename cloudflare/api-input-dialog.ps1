param()
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'Telegram API credentials'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object Drawing.Size(480, 210)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.ShowInTaskbar = $true
$form.Add_Shown({ $form.Activate(); [void]$id.Focus() })
function Add-Label($text, $y) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $text
    $label.AutoSize = $true
    $label.Location = New-Object Drawing.Point(22, $y)
    $form.Controls.Add($label)
}
Add-Label 'Paste the values from my.telegram.org (Ctrl+V).' 18
Add-Label 'App api_id' 55
Add-Label 'App api_hash' 110
$id = New-Object Windows.Forms.TextBox
$id.SetBounds(22, 76, 430, 24)
$hash = New-Object Windows.Forms.TextBox
$hash.SetBounds(22, 132, 430, 24)
$hash.UseSystemPasswordChar = $true
$ok = New-Object Windows.Forms.Button
$ok.Text = 'Continue'
$ok.SetBounds(266, 172, 90, 28)
$cancel = New-Object Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.SetBounds(362, 172, 90, 28)
$cancel.DialogResult = 'Cancel'
$ok.Add_Click({
    if ($id.Text.Trim() -notmatch '\A[0-9]+\z' -or $hash.Text.Trim() -notmatch '\A[0-9a-fA-F]{32}\z') {
        [void][Windows.Forms.MessageBox]::Show($form, 'Enter a numeric API ID and a 32-character API hash.', 'Check the values')
        return
    }
    $form.DialogResult = 'OK'
})
$form.Controls.AddRange(@($id, $hash, $ok, $cancel))
$form.AcceptButton = $ok
$form.CancelButton = $cancel
try {
    if ($form.ShowDialog() -ne 'OK') { exit 2 }
    $json = @{ apiId = $id.Text.Trim(); apiHash = $hash.Text.Trim() } | ConvertTo-Json -Compress
    [Console]::WriteLine($json)
} finally {
    $form.Dispose()
}
