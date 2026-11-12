export const client = {
  name: "Discount Tents",
  tagline: "PVC Tents & Jumping Castles in Nelspruit",
  description:
    "Affordable peg-and-pole PVC tents and jumping castles for weddings, birthdays, corporate events and community festivals across Mbombela and Mpumalanga.",
  phone: "+27 68 020 5277",
  whatsapp: "27680205277",
  email: "info@discounttents.co.za",
  address: "Mbombela, Mpumalanga",
  domain: "discounttents.co.za",
  googleMapsEmbed: "",

  services: [
    {
      title: "PVC Tents",
      description:
        "Sturdy peg-and-pole PVC tents in multiple sizes for weddings, festivals, corporate functions and family events. Delivery, setup and takedown included.",
      icon: "tent",
    },
    {
      title: "Jumping Castles",
      description:
        "Colourful jumping castles in safe, clean condition — perfect for kids' parties, school events, church gatherings and corporate family days.",
      icon: "sparkles",
    },
  ],

  testimonials: [
    {
      name: "Lerato M.",
      text: "Hired tents and a jumping castle for my son's birthday. The team delivered on time, set up quickly and everything looked great. Will definitely use Discount Tents again.",
      rating: 5,
    },
    {
      name: "Sipho K.",
      text: "Affordable, reliable and friendly service. They handled our community event in Nelspruit without any hassles from delivery to collection.",
      rating: 5,
    },
  ],

  gallery: [
    { src: "/images/gallery/1.jpg", alt: "Large white PVC tent set up for an outdoor event in Nelspruit" },
    { src: "/images/gallery/2.jpg", alt: "Peg-and-pole marquee ready for a wedding reception" },
    { src: "/images/gallery/3.jpg", alt: "Colourful jumping castle in use at a children's birthday party" },
    { src: "/images/gallery/4.jpg", alt: "Discount Tents crew setting up party equipment at a venue" },
  ],

  social: {
    facebook: null,
    instagram: null,
  },

  web3FormsKey: "XXXXX",
  cloudflareAnalyticsToken: "XXXXX",

  about:
    "Discount Tents supplies durable PVC tents and jumping castles for events of every size across Mbombela and the rest of Mpumalanga — from intimate family parties to large community festivals.",
  stats: [
    { value: "Mbombela", label: "Based in" },
    { value: "Mpumalanga", label: "Servicing" },
    { value: "Setup", label: "Included" },
  ],
  seo: {
    title: "Discount Tents | PVC Tents & Jumping Castles in Nelspruit",
    description:
      "Affordable PVC tents and jumping castles for hire in Nelspruit, Mbombela and across Mpumalanga. Delivery, setup and takedown included.",
    ogImage: "/images/hero.jpg",
  },
} as const;
