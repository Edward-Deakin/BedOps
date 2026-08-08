// Fetch all the main menu buttons
const new_world_button = document.getElementById("new_world");
const manage_world_button = document.getElementById("manage_world");
const upload_world_button = document.getElementById("upload_world");

// Event listener for clicks on the new world button
new_world_button.addEventListener("click", async (e) => {
    // Grab the world name
    const world_name_input = prompt("Please enter a name that you want to be associated with your server: ")
    if (!world_name_input) return;

    // Grab the crucial PIN
    const world_pin_input = prompt("To interact with your world within BedOps, you need a PIN. This PIN is securely hashed on Zerops so we never know it, it also stops people from gaining access to your BedOps instance.\n\nIMPORTANT: You cannot reset this PIN, please take note of it somewhere safe.\n\nPlease enter your PIN:")
    if (!world_pin_input) return;

    // Try to contact API
    try {
        // Send a request to the API
        const response = await fetch("api/create", {
            method: "POST",
            body: JSON.stringify({
                world_name: world_name_input,
                pin: world_pin_input
            }),
            headers: {
                "Content-Type": "application/json",
            }
        })

        // Parse the API into a useful JSON format
        const api_response = await response.json()
        console.log(api_response)

        if (response.ok) {
            alert(`Success! Your world is booting up.\n\nConnect to your project's IP address on Port: ${api_response.port}`);
        } else {
            alert(`Error: ${api_response.error}`);
        }

    } catch (error) {
        // Log error and alert user
        console.log(error);
        alert(`Sorry, something went wrong and your world was not created.\n\nError:\n${error.message}`)
    }
});

// Event listener for clicks on the manage world button
manage_world_button.addEventListener("click", async (e) => {
    const world_name_input = prompt("Enter the name of the world you want to stop/pause:");
    if (!world_name_input) return;

    const world_pin_input = prompt("Enter your secure PIN to verify ownership:");
    if (!world_pin_input) return;

    try {
        const response = await fetch("api/stop", {
            method: "POST",
            body: JSON.stringify({
                world_name: world_name_input,
                pin: world_pin_input
            }),
            headers: {
                "Content-Type": "application/json",
            }
        });

        const api_response = await response.json();

        if (response.ok) {
            alert(`Success! ${api_response.message}`);
        } else {
            alert(`Error: ${api_response.error || 'Failed to stop world.'}`);
        }
    } catch (error) {
        console.log(error);
        alert(`Sorry, something went wrong.\n\nError:\n${error.message}`);
    }
});

// Event listener for clicks on the upload world button
upload_world_button.addEventListener("click", async (e) => {
    alert("upload world feature in development!")
});